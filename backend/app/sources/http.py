from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .ipure_config import load_ipure_headers

COFFEE_HOST = "ip.net.coffee"
COFFEE_ORIGIN = f"https://{COFFEE_HOST}"
COFFEE_PAGE_URL = f"{COFFEE_ORIGIN}/ip/"
COFFEE_TRACE_URL = f"{COFFEE_ORIGIN}/cdn-cgi/trace"
IPURE_HOST = "ipure.dev"
IPURE_ORIGIN = f"https://{IPURE_HOST}"
IPURE_TIMEOUT_SECONDS = 30.0
IPURE_MAX_RESPONSE_BYTES = 1_000_000
GPT_PROBE_TARGETS = [
    {"name": "chatgpt.com", "url": "https://chatgpt.com/cdn-cgi/trace"},
    {"name": "api.openai.com", "url": "https://api.openai.com/v1/models"},
]

GLOBAL_PING_NODES = [
    {"code": "cn", "name": "上海", "node": "n01"},
    {"code": "hk", "name": "香港", "node": "n02"},
    {"code": "jp", "name": "东京", "node": "n03"},
    {"code": "sg", "name": "新加坡", "node": "n04"},
    {"code": "us", "name": "洛杉矶", "node": "n09"},
    {"code": "ca", "name": "温哥华", "node": "n11"},
    {"code": "de", "name": "法兰克福", "node": "n13"},
    {"code": "fr", "name": "巴黎", "node": "n15"},
]


class ResponseTooLarge(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("响应超过大小限制")
        self.status_code = status_code


class ProxyTransport:
    """Own the workspace proxy, HTTP limits and non-environment client policy."""

    def __init__(self, proxy_url: str, *, timeout_ms: int) -> None:
        proxy = httpx.URL(proxy_url)
        if (
            proxy.scheme != "http"
            or proxy.host != "127.0.0.1"
            or proxy.port is None
            or proxy.path not in {"", "/"}
        ):
            raise ValueError("节点采集器只接受工作区 Mihomo 的 127.0.0.1 mixed-port")
        self.proxy_url = str(proxy.copy_with(path=""))
        self.timeout_seconds = timeout_ms / 1000

    def deadline(self, cap_seconds: float) -> float:
        return min(self.timeout_seconds, cap_seconds)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=self.proxy_url,
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(self.timeout_seconds, 12)),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=5),
            headers={"User-Agent": "Mozilla/5.0 best-ip/0.3 (workspace Mihomo)"},
        )

    async def get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        timeout: float | httpx.Timeout,
        max_bytes: int = 5_000_000,
    ) -> httpx.Response:
        budget = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        async with asyncio.timeout(budget):
            headers = load_ipure_headers() if urlsplit(url).hostname == IPURE_HOST else None
            return await stream_bounded_get(
                client, url, timeout=timeout, max_bytes=max_bytes, headers=headers
            )


async def stream_bounded_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float | httpx.Timeout,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Read a response with a hard byte ceiling, so no caller can buffer unbounded data."""

    async with client.stream("GET", url, timeout=timeout, headers=headers) as response:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise ResponseTooLarge(response.status_code)
            body.extend(chunk)
        return httpx.Response(
            response.status_code,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            },
            content=bytes(body),
            request=response.request,
        )


def _validate_coffee_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Coffee 请求端口无效：{url}") from exc
    if parsed.scheme != "https" or parsed.hostname != COFFEE_HOST:
        raise ValueError(f"拒绝非 Coffee 同源请求：{url}")
    if parsed.username or parsed.password or port is not None or parsed.fragment:
        raise ValueError(f"拒绝带认证信息、自定义端口或 fragment 的 Coffee 请求：{url}")

    path = parsed.path or "/"
    query = parse_qs(parsed.query, keep_blank_values=True)
    if path in {"/ip/", "/cdn-cgi/trace"}:
        if query:
            raise ValueError(f"页面/trace 不允许 query：{url}")
        return
    if path.startswith("/api/ip/"):
        prefix = next(
            (
                item
                for item in (
                    "/api/ip/lookup/",
                    "/api/ip/related/",
                    "/api/ip/portscan/",
                    "/api/ip/pingcheck/",
                )
                if path.startswith(item)
            ),
            None,
        )
        if prefix is None:
            raise ValueError(f"拒绝未允许的 Coffee API path：{url}")
        value = unquote(path.removeprefix(prefix))
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"Coffee API IP 参数无效：{url}") from exc
        if prefix == "/api/ip/portscan/":
            if query != {"probe": ["0"]}:
                raise ValueError("portscan 只允许 probe=0 被动查询")
        elif query:
            raise ValueError(f"Coffee API 不允许额外 query：{url}")
        return
    if path == "/api/ping/global":
        hosts = query.get("host", [])
        nodes = query.get("node", [])
        if set(query) != {"host", "node"} or len(hosts) != 1:
            raise ValueError("global ping 必须只有一个 host")
        try:
            ipaddress.ip_address(unquote(hosts[0]))
        except ValueError as exc:
            raise ValueError("global ping host 无效") from exc
        allowed_nodes = {item["node"] for item in GLOBAL_PING_NODES}
        if len(nodes) != len(allowed_nodes) or set(nodes) != allowed_nodes:
            raise ValueError("global ping 必须使用固定八个 Coffee 节点")
        return
    raise ValueError(f"拒绝未允许的 Coffee path：{url}")


def _validate_gpt_url(url: str, target_name: str | None = None) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"GPT 探测端口无效：{url}") from exc
    expected = next(
        (
            target
            for target in GPT_PROBE_TARGETS
            if (target["name"] == target_name if target_name is not None else target["url"] == url)
        ),
        None,
    )
    if expected is None or url != expected["url"]:
        raise ValueError(f"拒绝未允许的 GPT 探测 URL：{url}")
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected["name"]
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != urlsplit(expected["url"]).path
    ):
        raise ValueError(f"拒绝未允许的 GPT 探测 URL：{url}")


def _validate_ipure_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"IPure 请求端口无效：{url}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != IPURE_HOST
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.fragment
        or parsed.path != "/api/lookup"
    ):
        raise ValueError(f"拒绝未允许的 IPure 请求：{url}")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"ip", "refresh"} or len(query.get("ip", [])) != 1:
        raise ValueError(f"IPure 查询必须且只能包含一个 IP 参数：{url}")
    if query.get("refresh") not in (None, ["1"]):
        raise ValueError(f"IPure refresh 参数只允许取 1：{url}")
    value = query["ip"][0]
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"IPure IP 参数无效：{url}") from exc
