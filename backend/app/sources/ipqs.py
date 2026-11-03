"""1ms IPQS (https://ipqs.1ms.dog) — multi-source IP quality aggregate.

`GET /api/check?ip=<ip>` streams NDJSON: one `start` event, one `src`/`bl` event per
upstream source or DNSBL, and a terminal `done` event carrying the page's verdict
(score 0..100, grade, consensus type, native/broadcast). Only `done` is consumed.

The page additionally runs two browser-only sources (ipinfo.io, ipwho.is). They add
geo/ASN/hostname but no type, anonymity flag or risk value, so they cannot move the
score; the `done` score is the same number the page shows.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .http import (
    IPQS_HOST,
    IPQS_MAX_RESPONSE_BYTES,
    IPQS_ORIGIN,
    ProxyTransport,
    ResponseTooLarge,
    _validate_ipqs_url,
    stream_bounded_get,
)
from .values import _clean_text

# Consensus access types the page can return; anything else is kept only as raw evidence.
IPQS_TYPES = ("residential", "mobile", "hosting", "business", "education", "government")
# Grade ladder from the page bundle: 85 A / 70 B / 50 C / 30 D / else F.
IPQS_GRADES = ("A", "B", "C", "D", "F")

_MAX_ATTEMPTS = 2
_ATTEMPT_SECONDS = 12.0
_DIRECT_TIMEOUT_SECONDS = 10.0
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    TimeoutError,
)
_UPSTREAM_ERRORS = {
    "rate": "1ms IPQS 查询频率受限",
    "bad_ip": "1ms IPQS 拒绝了该 IP 参数",
    "private": "1ms IPQS 不查询内网地址",
}


def ipqs_url(exit_ip: str) -> str:
    return f"{IPQS_ORIGIN}/api/check?{urlencode({'ip': str(ipaddress.ip_address(exit_ip))})}"


def ipqs_report_url(exit_ip: str) -> str:
    return f"{IPQS_ORIGIN}/zh-cn/?{urlencode({'ip': str(ipaddress.ip_address(exit_ip))})}"


async def request(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_ipqs_url(url)
    started = perf_counter()
    try:
        response = await _get_with_retry(transport, client, url, timeout_seconds, started)
    except ResponseTooLarge as exc:
        return _result(transport.proxy_url, url, started, status_code=exc.status_code,
                       error="1ms IPQS 响应超过大小限制", error_type="ResponseTooLarge")
    except _TRANSIENT_ERRORS as exc:
        fallback = await _direct(transport.proxy_url, url)
        if fallback is not None:
            # 兜底成功时留下代理路径为何失败：否则「全部走了直连」在数据里无迹可查。
            fallback["proxy_error"] = exc.__class__.__name__
            return fallback
        return _result(transport.proxy_url, url, started,
                       error="1ms IPQS 连接失败，请检查代理出口或稍后重试",
                       error_type="EnrichmentUnavailable", skipped=True)
    except Exception as exc:
        return _result(transport.proxy_url, url, started,
                       error=exc.__class__.__name__, error_type=exc.__class__.__name__)
    return _from_response(transport.proxy_url, url, started, response)


async def _get_with_retry(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    timeout_seconds: float,
    started: float,
) -> httpx.Response:
    for attempt in range(_MAX_ATTEMPTS):
        remaining = timeout_seconds - (perf_counter() - started)
        if remaining <= 0:
            raise TimeoutError
        last = attempt == _MAX_ATTEMPTS - 1
        try:
            response = await transport.get(
                client, url, timeout=min(remaining, _ATTEMPT_SECONDS),
                max_bytes=IPQS_MAX_RESPONSE_BYTES,
            )
        except _TRANSIENT_ERRORS:
            if last:
                raise
            continue
        if response.status_code != 429 or last:
            return response
        retry_after = response.headers.get("Retry-After", "")
        delay = float(retry_after) if retry_after.isdigit() else 1.0
        if delay >= timeout_seconds - (perf_counter() - started):
            return response
        await asyncio.sleep(delay)
    raise TimeoutError


async def _direct(proxy_url: str, url: str) -> dict[str, Any] | None:
    """Same query from the host when the node egress cannot reach 1ms; recorded as such.

    The verdict is keyed on the `ip` parameter, not on the caller, so the document is
    identical; only the vantage point differs, and the record says so.
    """

    started = perf_counter()
    timeout = httpx.Timeout(_DIRECT_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            response = await stream_bounded_get(
                client, url, timeout=timeout, max_bytes=IPQS_MAX_RESPONSE_BYTES
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
    if not response.is_success:
        code = _error_code(response)
        message = _UPSTREAM_ERRORS.get(code, "1ms IPQS 请求失败")
        skipped = response.status_code == 429 or code == "rate"
        return _result(proxy_url, url, started, **common,
                       error=f"{message}（HTTP {response.status_code}）",
                       error_type="EnrichmentUnavailable" if skipped else "HTTPStatusError",
                       skipped=skipped)
    report = parse_report(response.text, url)
    if report is None:
        return _result(proxy_url, url, started, **common,
                       error="1ms IPQS 响应缺少最终评分", error_type="ResponseParseError")
    return _result(proxy_url, url, started, **common, data=report)


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload.get("error") if isinstance(payload, dict) else None


def parse_report(body: str, url: str) -> dict[str, Any] | None:
    """Normalize the terminal `done` event; the 0..100 integer score is the hard requirement."""

    done = None
    for line in body.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("t") == "done":
            done = event
    if done is None:
        return None
    score = done.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        return None

    summary = done.get("summary") if isinstance(done.get("summary"), dict) else {}
    raw_type = _clean_text(summary.get("type")) or None
    grade = _clean_text(done.get("grade")) or None
    native = summary.get("native")
    dnsbl = summary.get("dnsbl") if isinstance(summary.get("dnsbl"), dict) else {}
    deductions = [
        {"key": str(part["k"]).removeprefix("p_"), "points": part["pts"], "detail": part.get("p")}
        for part in done.get("parts") or []
        if isinstance(part, dict) and isinstance(part.get("k"), str)
        and isinstance(part.get("pts"), int | float) and not isinstance(part.get("pts"), bool)
    ]
    query_ip = parse_qs(urlsplit(url).query)["ip"][0]
    return {
        "score": score,
        "grade": grade if grade in IPQS_GRADES else None,
        "type": raw_type if raw_type in IPQS_TYPES else None,
        "type_raw": raw_type,
        "native": native if isinstance(native, bool) else None,
        "country": _clean_text(summary.get("cc")) or None,
        "registered_country": _clean_text(summary.get("regCc")) or None,
        "registry_source": _clean_text(summary.get("regFrom")) or None,
        "risk": summary.get("risk") if isinstance(summary.get("risk"), int) else None,
        "flags": summary.get("flags") if isinstance(summary.get("flags"), dict) else None,
        "dnsbl_listed": dnsbl.get("listed") if isinstance(dnsbl.get("listed"), int) else None,
        "dnsbl_checked": dnsbl.get("checked") if isinstance(dnsbl.get("checked"), int) else None,
        "deductions": deductions,
        "uses": done.get("uses") if isinstance(done.get("uses"), dict) else None,
        "notes": done.get("notes") if isinstance(done.get("notes"), list) else None,
        "checked_at": _clean_text(done.get("at")) or None,
        "cached": done.get("cached") if isinstance(done.get("cached"), bool) else None,
        "report_url": ipqs_report_url(query_ip),
    }


def ipqs_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a request result into the node record's `ipqs_*` fields; keys always present."""

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {
        "ipqs_score": data.get("score"),
        "ipqs_grade": data.get("grade"),
        "ipqs_type": data.get("type"),
        "ipqs_native": data.get("native"),
        "ipqs_report_url": data.get("report_url"),
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
    direct_fallback: bool = False,
) -> dict[str, Any]:
    return {
        "url": url,
        "attempted": True,
        "via_mihomo": not direct_fallback,
        "proxy_url": proxy_url,
        "target_host": IPQS_HOST,
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
