"""ipx.al (https://ipx.al) — IP health score, native verdict and type votes.

`GET /json` answers for the caller: asked through the node proxy it reports the node's own
egress in one request — no exit IP needed first, so it starts together with the trace
instead of queueing behind it. `GET /api/report/{ip}` is the same document keyed on an
address; it is the host-side fallback, used only after the trace has named the exit IP.

The proxied answer is accepted only when its `ip` equals the trace exit IP: multi-exit
nodes may use another address per connection, and then the report describes a different
IP. Only `health.score` / `health.grade` reach the record; the rest stays as evidence.
"""

from __future__ import annotations

import ipaddress
from time import perf_counter
from typing import Any
from urllib.parse import quote

import httpx

from .http import (
    IPX_HOST,
    IPX_MAX_RESPONSE_BYTES,
    IPX_ORIGIN,
    ProxyTransport,
    ResponseTooLarge,
    _validate_ipx_url,
    stream_bounded_get,
)
from .values import _clean_text

# Grade ladder from https://ipx.al/about: 85 极佳 / 70 良好 / 50 一般 / 30 较差 / else 高风险.
IPX_GRADES = ("极佳", "良好", "一般", "较差", "高风险")

_DIRECT_TIMEOUT_SECONDS = 15.0


def ipx_report_url(exit_ip: str) -> str:
    return f"{IPX_ORIGIN}/api/report/{quote(str(ipaddress.ip_address(exit_ip)), safe='')}"


def ipx_page_url(exit_ip: str) -> str:
    return f"{IPX_ORIGIN}/{quote(str(ipaddress.ip_address(exit_ip)), safe='')}"


async def request(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_ipx_url(url)
    started = perf_counter()
    try:
        response = await transport.get(
            client, url, timeout=timeout_seconds, max_bytes=IPX_MAX_RESPONSE_BYTES
        )
    except ResponseTooLarge as exc:
        return _result(transport.proxy_url, url, started, status_code=exc.status_code,
                       error="ipx.al 响应超过大小限制", error_type="ResponseTooLarge")
    except Exception as exc:
        return _result(transport.proxy_url, url, started,
                       error="ipx.al 连接失败，请检查代理出口或稍后重试",
                       error_type=exc.__class__.__name__, skipped=True)
    return _from_response(transport.proxy_url, url, started, response)


async def for_exit_ip(proxy_url: str, proxied: dict[str, Any], exit_ip: str) -> dict[str, Any]:
    """Keep the proxied self-report when it describes `exit_ip`; otherwise ask by IP from the host.

    The by-IP report is keyed on the address, not the caller, so the host vantage point
    yields the same document; the record says so (`direct_fallback`, `proxy_error`).
    """

    data = proxied.get("data") if isinstance(proxied.get("data"), dict) else {}
    if proxied.get("ok") and _same_ip(data.get("ip"), exit_ip):
        return proxied
    reason = (
        "IPMismatch" if proxied.get("ok") else proxied.get("error_type") or "EnrichmentUnavailable"
    )
    fallback = await _direct(proxy_url, ipx_report_url(exit_ip))
    if fallback is not None:
        fallback["proxy_error"] = reason
        return fallback
    if proxied.get("ok"):
        return {**proxied, "ok": False, "data": None, "error_type": "IPMismatch",
                "error": "ipx.al 回显的出口 IP 与 trace 不一致，且按 IP 查询失败"}
    return proxied


async def _direct(proxy_url: str, url: str) -> dict[str, Any] | None:
    _validate_ipx_url(url)
    started = perf_counter()
    timeout = httpx.Timeout(_DIRECT_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            response = await stream_bounded_get(
                client, url, timeout=timeout, max_bytes=IPX_MAX_RESPONSE_BYTES
            )
    except (httpx.HTTPError, ResponseTooLarge, TimeoutError):
        return None
    result = _from_response(proxy_url, url, started, response, direct_fallback=True)
    return result if result["ok"] else None


def _from_response(
    proxy_url: str,
    url: str,
    started: float,
    response: httpx.Response,
    *,
    direct_fallback: bool = False,
) -> dict[str, Any]:
    common = {"status_code": response.status_code, "direct_fallback": direct_fallback}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not response.is_success:
        message = payload.get("error") if isinstance(payload, dict) else None
        rate_limited = response.status_code == 429
        return _result(proxy_url, url, started, **common,
                       error=f"ipx.al 请求失败（HTTP {response.status_code}"
                             f"{'：' + _clean_text(message)[:80] if message else ''}）",
                       error_type="EnrichmentUnavailable" if rate_limited else "HTTPStatusError",
                       skipped=rate_limited)
    report = parse_report(payload)
    if report is None:
        return _result(proxy_url, url, started, **common,
                       error="ipx.al 响应缺少健康分", error_type="ResponseParseError")
    return _result(proxy_url, url, started, **common, data=report)


def parse_report(payload: Any) -> dict[str, Any] | None:
    """Normalize a report; a valid `ip` and an integer 0..100 `health.score` are required."""

    if not isinstance(payload, dict):
        return None
    try:
        ip = str(ipaddress.ip_address(payload.get("ip")))
    except (TypeError, ValueError):
        return None
    health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
    score = health.get("score")
    if not _int_between(score, 0, 100):
        return None
    grade = _clean_text(health.get("grade")) or None
    native = payload.get("native") if isinstance(payload.get("native"), dict) else {}
    verdict = native.get("verdict")
    kind = payload.get("type") if isinstance(payload.get("type"), dict) else {}
    coverage = health.get("coverage") if isinstance(health.get("coverage"), dict) else {}
    items = [
        {
            "code": _clean_text(item.get("code")) or None,
            "label": _clean_text(item.get("label")),
            "points": item["pts"],
        }
        for item in health.get("items") or []
        if isinstance(item, dict) and _clean_text(item.get("label"))
        and isinstance(item.get("pts"), int | float) and not isinstance(item.get("pts"), bool)
    ]
    return {
        "ip": ip,
        "score": score,
        "grade": grade if grade in IPX_GRADES else None,
        "grade_raw": grade,
        "ceiling": health.get("ceiling") if _int_between(health.get("ceiling"), 0, 100) else None,
        "ip_type": _clean_text(health.get("ipType")) or None,
        "type_verdict": _clean_text(kind.get("verdict")) or None,
        "native": {"native": True, "broadcast": False}.get(verdict),
        "registered_country": _clean_text(native.get("regCountry")) or None,
        "items": items,
        "model": _clean_text(health.get("model")) or None,
        "coverage_low": coverage.get("low") if isinstance(coverage.get("low"), bool) else None,
        "generated_at": _clean_text(payload.get("generatedAt")) or None,
        "report_url": ipx_page_url(ip),
    }


def ipx_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a request result into the record's score fields; keys always present."""

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "ipx_score": data.get("score"),
        "ipx_grade": data.get("grade"),
        "ipx_report_url": data.get("report_url"),
    }


def _int_between(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _same_ip(left: Any, right: Any) -> bool:
    try:
        return ipaddress.ip_address(left) == ipaddress.ip_address(right)
    except (TypeError, ValueError):
        return False


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
    direct_fallback: bool = False,
) -> dict[str, Any]:
    return {
        "url": url,
        "attempted": True,
        "via_mihomo": not direct_fallback,
        "proxy_url": proxy_url,
        "target_host": IPX_HOST,
        "ok": status_code is not None and 200 <= status_code < 300 and error is None,
        "status_code": status_code,
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "data": data,
        "error": error,
        "error_type": error_type,
        "location": None,
        "skipped": skipped,
        "direct_fallback": direct_fallback,
    }
