from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

RELAY_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]{16,512}")
RELAY_ERROR_REASONS = {
    401: "relay_unauthorized",
    413: "relay_response_too_large",
    502: "relay_unreachable",
    503: "relay_not_configured",
    504: "relay_timeout",
}


class SubscriptionError(ValueError):
    """订阅错误。reason 是固定安全原因码，可进日志而不泄露订阅地址。"""

    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        self.reason = reason
        super().__init__(message)


def subscription_failure_reason(exc: BaseException) -> str:
    """把订阅异常收敛成固定安全原因码，未知情况统一为 unknown。"""

    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,48}", reason):
        return reason
    return "unknown"


# 值得换个出口重试的直连失败原因：它们说明「这个出口到不了订阅主机」，而不是「订阅本身有问题」。
# - http_status_403：订阅主机按出口网络拒绝的实测特征（Azure 与 Cloudflare 两个方向都实测到过）；
# - direct_timeout / direct_unreachable：直连连不上（黑洞、重置、TLS/协议中断）。这类主机在
#   「一直走中继」的旧策略下本来是被中继救回来的，直连优先不能把它变成硬失败。
# 不在集合里的码（dns_*、redirect_*、response_too_large、http_status_401/404…）说明问题在订阅或
# 地址本身，换出口不会变好，重试只会把一次输入错误变成两次网络往返加一个更难解释的原因码。
EGRESS_FALLBACK_REASONS = frozenset({"http_status_403", "direct_timeout", "direct_unreachable"})


@dataclass
class SubscriptionSource:
    """本次抓取实际用的出口，只用于日志归因，不参与任何安全判定。

    `value` 只由 `download_subscription` 写成 `direct` / `relay`；留成 `None` 表示没有任何
    实现回写过它（例如注入的替身），调用方据此打 `unknown` 而不是假装知道。
    `fallback_reason` 只在直连失败、改走中继时记下直连那一次的固定原因码。
    """

    value: str | None = None
    fallback_reason: str | None = None
@dataclass(frozen=True)
class SubscriptionRelay:
    """Worker 中继：把某一次抓取的出口换成 Cloudflare 边缘，而不是扫描 runner。

    它只为「某个出口被订阅主机按网络拒绝」这类情况存在，不能预设谁更通：实测同一台
    订阅主机可以只拒 Azure（runner 403 / Worker 200），也可以只拒 Cloudflare
    （runner 200 / Worker 403，被 managed challenge 挡住）。谁先谁后由
    `download_subscription` 按实际结果决定并记进 `SubscriptionSource`；runner 仍逐跳
    校验目标必须是公网地址。
    """

    url: str
    token: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in self.url)
        ):
            raise SubscriptionError("订阅中继地址无效", reason="relay_url_invalid")
        if not RELAY_TOKEN_PATTERN.fullmatch(self.token):
            raise SubscriptionError("订阅中继令牌无效", reason="relay_token_invalid")


@dataclass(frozen=True)
class _RelayResult:
    status: int
    location: str | None
    body: bytes | None


MIHOMO_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
PUBLIC_DOH_URL = "https://1.1.1.1/dns-query"
SUBSCRIPTION_METADATA_PATTERNS = (
    re.compile(r"^(?:剩余流量|流量剩余)[：:]\s*\d+(?:\.\d+)?\s*[KMGTPE]?B\b", re.I),
    re.compile(r"^(?:距离下次重置(?:剩余)?|下次重置)[：:]\s*\d+\s*天"),
    re.compile(r"^(?:套餐到期|到期时间)[：:]\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"),
    re.compile(r"^官网[：:]\s*(?:https?://)?(?:[\w-]+\.)+[\w-]+", re.I),
    re.compile(r"^群组[：:]\s*(?:https?://)?(?:t\.me/|(?:[\w-]+\.)+[\w-]+)", re.I),
)


def is_subscription_metadata(proxy: dict[str, Any]) -> bool:
    name = str(proxy.get("name") or "").strip()
    return any(pattern.match(name) for pattern in SUBSCRIPTION_METADATA_PATTERNS)


async def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SubscriptionError("订阅地址必须是有效的 HTTP 或 HTTPS URL", reason="non_http_scheme")
    if parsed.username or parsed.password:
        raise SubscriptionError("订阅地址不支持 URL 用户名或密码", reason="url_credentials")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise SubscriptionError("订阅地址不能指向本机", reason="localhost_target")

    hostname_is_ip = False
    try:
        addresses = [ipaddress.ip_address(hostname)]
        hostname_is_ip = True
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = list({ipaddress.ip_address(item[4][0]) for item in infos})
        except (OSError, ValueError) as exc:
            raise SubscriptionError("无法解析订阅地址的域名", reason="dns_unresolved") from exc

    if not hostname_is_ip and any(address in MIHOMO_FAKE_IP_NETWORK for address in addresses):
        addresses = await _resolve_with_public_doh(hostname)
    if not addresses or any(not address.is_global for address in addresses):
        raise SubscriptionError("订阅地址不能指向内网、回环或保留地址", reason="dns_not_global")


async def _resolve_with_public_doh(
    hostname: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            responses = await asyncio.gather(
                client.get(
                    PUBLIC_DOH_URL,
                    params={"name": hostname, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                ),
                client.get(
                    PUBLIC_DOH_URL,
                    params={"name": hostname, "type": "AAAA"},
                    headers={"Accept": "application/dns-json"},
                ),
                return_exceptions=True,
            )
            addresses = []
            for response in responses:
                if isinstance(response, Exception) or response.status_code >= 400:
                    continue
                try:
                    answers = response.json().get("Answer", [])
                except (ValueError, TypeError):
                    continue
                for item in answers:
                    if not isinstance(item, dict) or item.get("type") not in {1, 28}:
                        continue
                    try:
                        addresses.append(ipaddress.ip_address(item["data"]))
                    except (KeyError, ValueError, TypeError):
                        continue
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise SubscriptionError("无法通过公共 DNS 核验订阅地址", reason="doh_unavailable") from exc

    if not addresses:
        raise SubscriptionError("订阅地址没有可用的公网 DNS 记录", reason="dns_no_public_records")
    return list(dict.fromkeys(addresses))


async def download_subscription(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    relay: SubscriptionRelay | None = None,
    source: SubscriptionSource | None = None,
) -> bytes:
    """抓订阅：直连优先，只有直连以「这个出口到不了订阅主机」的方式失败（403 或连接层失败）
    且配了中继时，才改走 Worker 中继再抓一次。

    两种出口都有真实反例，所以策略不能写死成「一直走中继」或「一直直连」。回退只认
    `EGRESS_FALLBACK_REASONS`：DNS、重定向、超限、401/404 等其它失败照原样抛出，免得把
    「订阅本身有问题」误判成「换个出口就好了」。实际用的是哪个出口写进 `source`，
    调用方据此打印 `subscription_source`。
    """

    recorder = source if source is not None else SubscriptionSource()
    try:
        content = await _download_direct(
            url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
    except SubscriptionError as exc:
        if relay is None or exc.reason not in EGRESS_FALLBACK_REASONS:
            raise
        recorder.value = "relay"
        recorder.fallback_reason = exc.reason
        return await _download_via_relay(
            url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            relay=relay,
        )
    recorder.value = "direct"
    return content


async def _download_direct(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> bytes:
    current_url = url.strip()
    timeout = httpx.Timeout(timeout_seconds)
    headers = {
        "User-Agent": "clash.meta",
        "Accept": "application/yaml,text/yaml,text/plain,*/*",
    }

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(6):
                await validate_public_url(current_url)
                async with client.stream("GET", current_url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SubscriptionError(
                                "订阅地址返回了无目标的重定向", reason="redirect_without_location"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code >= 400:
                        raise SubscriptionError(
                            f"下载订阅失败：HTTP {response.status_code}",
                            reason=f"http_status_{response.status_code}",
                        )

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise SubscriptionError(
                                "订阅内容超过允许大小", reason="response_too_large"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
    except httpx.TimeoutException as exc:
        # httpx 的超时异常是 TransportError 的子类，必须先于它捕获。
        raise SubscriptionError("直连订阅超时", reason="direct_timeout") from exc
    except httpx.TransportError as exc:
        raise SubscriptionError("直连订阅失败", reason="direct_unreachable") from exc

    raise SubscriptionError("订阅地址重定向次数过多", reason="redirect_limit")



async def _download_via_relay(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
    relay: SubscriptionRelay,
) -> bytes:
    """经 Worker 中继抓取订阅；每一跳都按直连路径同样校验必须是公网地址。"""

    current_url = url.strip()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(6):
            await validate_public_url(current_url)
            result = await _relay_fetch(client, relay, current_url, max_bytes=max_bytes)
            if result.location is not None:
                current_url = urljoin(current_url, result.location)
                continue
            if result.status >= 400:
                raise SubscriptionError(
                    f"下载订阅失败：HTTP {result.status}",
                    reason=f"http_status_{result.status}",
                )
            if result.body is None:
                raise SubscriptionError("订阅中继没有返回内容", reason="relay_empty_body")
            return result.body

    raise SubscriptionError("订阅地址重定向次数过多", reason="redirect_limit")


async def _relay_fetch(
    client: httpx.AsyncClient,
    relay: SubscriptionRelay,
    target: str,
    *,
    max_bytes: int,
) -> _RelayResult:
    limit = max_bytes * 2 + 65536
    try:
        async with client.stream(
            "POST",
            relay.url,
            json={"url": target},
            headers={"X-Best-IP-Relay-Token": relay.token},
        ) as response:
            status = response.status_code
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise SubscriptionError(
                        "订阅中继响应超过允许大小", reason="relay_response_too_large"
                    )
                chunks.append(chunk)
    except httpx.TimeoutException as exc:
        raise SubscriptionError("订阅中继请求超时", reason="relay_timeout") from exc
    except httpx.HTTPError as exc:
        raise SubscriptionError("无法连接订阅中继", reason="relay_unreachable") from exc

    if status != 200:
        raise SubscriptionError(
            f"订阅中继返回 HTTP {status}",
            reason=RELAY_ERROR_REASONS.get(status, "relay_error"),
        )

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SubscriptionError(
            "订阅中继响应不是有效 JSON", reason="relay_response_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise SubscriptionError("订阅中继响应结构无效", reason="relay_response_invalid")

    upstream_status = payload.get("status")
    location = payload.get("location")
    encoded = payload.get("body_b64")
    if type(upstream_status) is not int or not 100 <= upstream_status <= 599:
        raise SubscriptionError("订阅中继状态码无效", reason="relay_response_invalid")
    if location is not None and (not isinstance(location, str) or len(location) > 4096):
        raise SubscriptionError("订阅中继重定向无效", reason="relay_response_invalid")
    if encoded is None:
        return _RelayResult(upstream_status, location, None)
    if not isinstance(encoded, str):
        raise SubscriptionError("订阅中继内容无效", reason="relay_response_invalid")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SubscriptionError("订阅中继内容无法解码", reason="relay_response_invalid") from exc
    if len(body) > max_bytes:
        raise SubscriptionError("订阅内容超过允许大小", reason="response_too_large")
    return _RelayResult(upstream_status, None, body)


def parse_subscription(content: bytes, *, max_nodes: int) -> list[dict[str, Any]]:
    try:
        document = yaml.safe_load(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SubscriptionError(
            "订阅不是有效的 UTF-8 Mihomo YAML", reason="body_not_utf8_yaml"
        ) from exc

    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise SubscriptionError(
            "订阅必须是顶部含 proxies 列表的 Mihomo YAML", reason="body_not_mihomo_yaml"
        )

    proxies = document["proxies"]
    if not proxies:
        raise SubscriptionError("订阅中没有节点", reason="body_no_proxies")
    detectable_count = sum(
        1
        for proxy in proxies
        if not isinstance(proxy, dict) or not is_subscription_metadata(proxy)
    )
    if detectable_count > max_nodes:
        raise SubscriptionError(
            f"订阅含 {detectable_count} 个节点，超过上限 {max_nodes}", reason="body_too_many_nodes"
        )

    names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, proxy in enumerate(proxies, start=1):
        if not isinstance(proxy, dict):
            raise SubscriptionError(f"第 {index} 个节点配置不是对象", reason="node_not_object")
        if is_subscription_metadata(proxy):
            normalized.append(dict(proxy))
            continue
        name = proxy.get("name")
        proxy_type = proxy.get("type")
        if not isinstance(name, str) or not name.strip():
            raise SubscriptionError(f"第 {index} 个节点缺少有效名称", reason="node_name_missing")
        if not isinstance(proxy_type, str) or not proxy_type.strip():
            raise SubscriptionError(f"第 {index} 个节点缺少有效类型", reason="node_type_missing")
        if proxy_type.strip().lower() in {"direct", "reject"}:
            raise SubscriptionError(
                f"第 {index} 个节点使用了不允许的直连/拒绝类型", reason="node_type_forbidden"
            )
        if name in names:
            raise SubscriptionError(f"第 {index} 个节点名称重复", reason="node_name_duplicate")
        names.add(name)
        normalized.append(dict(proxy))

    return normalized
