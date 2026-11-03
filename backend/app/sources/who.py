"""1ms who (https://who.1ms.dog) — what a website sees of the caller.

`GET /api/me` answers for the connection itself: the address Cloudflare saw (`ip`), its
edge geo/ASN view (`cf.country` / `region` / `city` / `asn` / `asOrganization`) and the
reverse DNS name (`ptr`). Asked through the node proxy it describes the node's egress,
so it is the primary source for the geo and ISP groups and for the server name (rDNS).

There is deliberately no host-side fallback: the answer is keyed on the caller, and a
direct query would describe the scan host instead of the node.
"""

from __future__ import annotations

import ipaddress
from time import perf_counter
from typing import Any

import httpx

from .http import (
    WHO_HOST,
    WHO_MAX_RESPONSE_BYTES,
    ProxyTransport,
    ResponseTooLarge,
    _validate_who_url,
)
from .values import _clean_text


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
