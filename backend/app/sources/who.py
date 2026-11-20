"""1ms who (https://who.1ms.dog) — what a website sees of the caller.

`GET /api/me` answers for the connection itself: the address Cloudflare saw (`ip`), its
edge geo/ASN view (`cf.country` / `region` / `city` / `asn` / `asOrganization`) and the
reverse DNS name (`ptr`). Asked through the node proxy it describes the node's egress,
so it is the primary source for the geo and ISP groups and for the server name (rDNS).

There is deliberately no host-side fallback: the answer is keyed on the caller, and a
direct query would describe the scan host instead of the node.

`request_family` asks the same endpoint through one of who.1ms.dog's own addresses (an A
or an AAAA record) instead of its name, so the node has to leave over that IP family. That
is how a dual-stack node shows both its IPv4 and its IPv6 egress without adding a new site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from time import perf_counter
from typing import Any

import h11
import httpx

from .http import (
    WHO_HOST,
    WHO_MAX_RESPONSE_BYTES,
    WHO_ME_URL,
    ProxyTransport,
    ResponseTooLarge,
    _validate_who_url,
)
from .values import _clean_text

_ME_PATH = "/api/me"
_USER_AGENT = "Mozilla/5.0 best-ip/0.3 (workspace Mihomo)"


async def resolve_addresses() -> dict[int, str]:
    """who.1ms.dog 的一个 IPv4 和一个 IPv6 地址（经公共 DoH，不受本机 fake-ip 影响）。

    解析不到就返回空：双栈探测整体跳过，节点照旧只按默认出口检测一行。
    """

    from ..subscription import _resolve_with_public_doh

    try:
        addresses = await _resolve_with_public_doh(WHO_HOST)
    except Exception:
        return {}
    chosen: dict[int, str] = {}
    for address in addresses:
        if address.is_global:
            chosen.setdefault(address.version, str(address))
    return chosen


async def request_family(
    proxy_url: str,
    address: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """经节点代理连 who.1ms.dog 的某个地址，回答「这个节点走 IPv4/IPv6 时出口是谁」。

    目标写成 IP 字面量，节点只能按这一族出站；TLS 仍以 who.1ms.dog 校验证书。
    有的节点会按 SNI 重新解析域名、改走另一族——回显 IP 的族与所连地址不一致时不采信，
    这个节点就只显示一行。httpx 的代理隧道总拿 URL 主机当 SNI，所以这里自己发 CONNECT。
    """

    version = ipaddress.ip_address(address).version
    started = perf_counter()
    label = f"1ms who（IPv{version}）"
    try:
        async with asyncio.timeout(timeout_seconds):
            status_code, body = await _get_via_address(proxy_url, address)
    except ResponseTooLarge as exc:
        return _result(proxy_url, WHO_ME_URL, started, status_code=exc.status_code,
                       error=f"{label} 响应超过大小限制", error_type="ResponseTooLarge")
    except Exception as exc:
        return _result(proxy_url, WHO_ME_URL, started,
                       error=f"{label} 连接失败，节点可能没有这一族出口",
                       error_type=exc.__class__.__name__, skipped=True)
    if not 200 <= status_code < 300:
        return _result(proxy_url, WHO_ME_URL, started, status_code=status_code,
                       error=f"{label} 请求失败（HTTP {status_code}）",
                       error_type="HTTPStatusError")
    try:
        report = parse_me(httpx.Response(status_code, content=body).json())
    except ValueError:
        report = None
    if report is None:
        return _result(proxy_url, WHO_ME_URL, started, status_code=status_code,
                       error=f"{label} 响应缺少出口 IP", error_type="ResponseParseError")
    if ipaddress.ip_address(report["ip"]).version != version:
        return _result(proxy_url, WHO_ME_URL, started, status_code=status_code,
                       error=f"节点没有按 IPv{version} 出站", error_type="FamilyMismatch",
                       skipped=True)
    return _result(proxy_url, WHO_ME_URL, started, status_code=status_code, data=report)


async def _get_via_address(proxy_url: str, address: str) -> tuple[int, bytes]:
    proxy = httpx.URL(proxy_url)
    if proxy.host != "127.0.0.1" or proxy.port is None:
        raise ValueError("双栈探测只走工作区 Mihomo 的 127.0.0.1 mixed-port")
    authority = f"[{address}]:443" if ":" in address else f"{address}:443"
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        if head.split(b" ", 2)[1:2] != [b"200"]:
            raise ConnectionError("Mihomo 拒绝了 CONNECT")
        await writer.start_tls(ssl.create_default_context(), server_hostname=WHO_HOST)
        connection = h11.Connection(h11.CLIENT)
        request = h11.Request(
            method="GET",
            target=_ME_PATH,
            headers=[
                ("Host", WHO_HOST),
                ("User-Agent", _USER_AGENT),
                ("Accept", "application/json"),
                ("Connection", "close"),
            ],
        )
        writer.write(connection.send(request) + connection.send(h11.EndOfMessage()))
        await writer.drain()
        status_code: int | None = None
        body = bytearray()
        while True:
            event = connection.next_event()
            if event is h11.NEED_DATA:
                connection.receive_data(await reader.read(65536))
            elif isinstance(event, h11.Response):
                status_code = event.status_code
            elif isinstance(event, h11.Data):
                if len(body) + len(event.data) > WHO_MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge(status_code or 0)
                body.extend(event.data)
            elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                break
        if status_code is None:
            raise ConnectionError("连接在响应头之前关闭")
        return status_code, bytes(body)
    finally:
        writer.close()


async def request(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_who_url(url)
    started = perf_counter()
    try:
        response = await transport.get(
            client, url, timeout=timeout_seconds, max_bytes=WHO_MAX_RESPONSE_BYTES
        )
    except ResponseTooLarge as exc:
        return _result(transport.proxy_url, url, started, status_code=exc.status_code,
                       error="1ms who 响应超过大小限制", error_type="ResponseTooLarge")
    except Exception as exc:
        return _result(transport.proxy_url, url, started,
                       error="1ms who 连接失败，请检查代理出口或稍后重试",
                       error_type=exc.__class__.__name__, skipped=True)
    if not response.is_success:
        return _result(transport.proxy_url, url, started, status_code=response.status_code,
                       error=f"1ms who 请求失败（HTTP {response.status_code}）",
                       error_type="HTTPStatusError")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    report = parse_me(payload)
    if report is None:
        return _result(transport.proxy_url, url, started, status_code=response.status_code,
                       error="1ms who 响应缺少出口 IP", error_type="ResponseParseError")
    return _result(transport.proxy_url, url, started, status_code=response.status_code,
                   data=report)


def parse_me(payload: Any) -> dict[str, Any] | None:
    """Normalize `/api/me`; a valid `ip` is the hard requirement, everything else may be null.

    Request headers the endpoint echoes back (`h`) and TLS details are not kept.
    """

    if not isinstance(payload, dict):
        return None
    try:
        ip = str(ipaddress.ip_address(payload.get("ip")))
    except (TypeError, ValueError):
        return None
    cf = payload.get("cf") if isinstance(payload.get("cf"), dict) else {}
    country_code = _clean_text(cf.get("country")).upper()
    asn = cf.get("asn")
    return {
        "ip": ip,
        "country_code": country_code if len(country_code) == 2 and country_code.isalpha() else None,
        "region": _clean_text(cf.get("region")) or None,
        "city": _clean_text(cf.get("city")) or None,
        "continent": _clean_text(cf.get("continent")) or None,
        "timezone": _clean_text(cf.get("timezone")) or None,
        "asn": asn if isinstance(asn, int) and not isinstance(asn, bool) and asn > 0 else None,
        "as_org": _clean_text(cf.get("asOrganization")) or None,
        "colo": _clean_text(cf.get("colo")) or None,
        "ptr": _clean_text(payload.get("ptr")) or None,
    }


def _result(
    proxy_url: str,
    url: str,
    started: float,
    *,
    status_code: int | None = None,
    data: dict[str, Any] | None = None,
    error: str | None = None,
    error_type: str | None = None,
    skipped: bool = False,
) -> dict[str, Any]:
    return {
        "url": url,
        "attempted": True,
        "via_mihomo": True,
        "proxy_url": proxy_url,
        "target_host": WHO_HOST,
        "ok": status_code is not None and 200 <= status_code < 300 and error is None,
        "status_code": status_code,
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "data": data,
        "error": error,
        "error_type": error_type,
        "location": None,
        "skipped": skipped,
    }
