from __future__ import annotations

import asyncio
import ipaddress
import re
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx

from .http import (
    IPURE_HOST,
    IPURE_MAX_RESPONSE_BYTES,
    IPURE_ORIGIN,
    ProxyTransport,
    ResponseTooLarge,
    _validate_ipure_url,
    stream_bounded_get,
)
from .ipure_config import load_ipure_headers
from .values import _clean_text, _numeric_score

# 场景 id 与 https://ipure.dev/docs/api 的 scenarios[].id 一一对应。
IPURE_SCENARIOS = {
    "ai": "AI 服务",
    "social": "社交平台注册",
    "streaming": "流媒体 / 短视频",
    "gaming": "游戏平台",
    "ecommerce": "跨境电商",
    "email": "邮件发送",
}

# 官网要求：这些档位下 score 不代表可用性，引用时不能当作结论。
#
# 其中 restricted（地区受限）不是"分数低"，而是"该地区拿不到分"，因此它不参与
# 0..100 刻度，改用一个刻度外的哨兵值 -1 落盘；前端把 -1 当成"受限"直接展示数字，
# 不做任何档位文案映射。not_applicable / unusable 仍然保留上游给的分数。
IPURE_RESTRICTED_LEVEL = "restricted"
IPURE_RESTRICTED_SCORE = -1

IPURE_MAX_ATTEMPTS = 4

# 单次尝试的硬上限。未收录 IP 的实时多源查询很慢，若让一次尝试吃满整个时间预算，
# 卡住的连接会挤掉后面所有重试；限制单次时长才能让重试真正发生。
IPURE_MAX_ATTEMPT_SECONDS = 10.0
# 直连兜底的硬上限。本机直连 IPure 实测在 1 秒级返回：13 次成功兜底全部落在
# 428–1298ms（中位 596ms），20 秒的旧上限只是在替一个注定超时的请求干等。
IPURE_DIRECT_TIMEOUT_SECONDS = 5.0

# 未收录 IP 的实时多源查询会偶发被对端直接断开（RemoteProtocolError / IncompleteRead），
# 这类瞬时错误重试即可恢复，不应当成"拿不到分数"。
_IPURE_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    TimeoutError,
)


async def request(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_ipure_url(url)
    started = perf_counter()
    try:
        response = await _get_with_retry(
            transport,
            client,
            url,
            timeout_seconds=timeout_seconds,
            started=started,
        )
        if 300 <= response.status_code < 400:
            return _ipure_request_result(
                transport.proxy_url,
                url,
                started,
                status_code=response.status_code,
                error="IPure 请求被重定向，已按策略拒绝跟随",
                error_type="RedirectRejected",
                location=response.headers.get("location"),
            )
        payload = _json_payload(response)
        if not response.is_success:
            message, error_type, skipped = _response_error(response, payload)
            return _ipure_request_result(
                transport.proxy_url,
                url,
                started,
                status_code=response.status_code,
                error=message,
                error_type=error_type,
                skipped=skipped,
                location=response.headers.get("location"),
                report_url=_report_url(payload),
                budget_remaining=_budget_remaining(response),
            )
        report = _parse_ipure_report(payload)
        if report is None:
            return _ipure_request_result(
                transport.proxy_url,
                url,
                started,
                status_code=response.status_code,
                error="IPure API 响应缺少纯净度总分",
                error_type="ResponseParseError",
                budget_remaining=_budget_remaining(response),
            )
        return _ipure_request_result(
            transport.proxy_url,
            url,
            started,
            status_code=response.status_code,
            data=report,
            budget_remaining=_budget_remaining(response),
        )
    except ResponseTooLarge as exc:
        return _ipure_request_result(
            transport.proxy_url,
            url,
            started,
            status_code=exc.status_code,
            error="IPure 响应超过大小限制",
            error_type="ResponseTooLarge",
        )
    except _IPURE_TRANSIENT_ERRORS:
        fallback = await _direct_report(transport.proxy_url, url, timeout_seconds=timeout_seconds)
        if fallback is not None:
            return fallback
        return _ipure_request_result(
            transport.proxy_url,
            url,
            started,
            error="IPure 连接失败，请检查代理出口或稍后重试",
            error_type="EnrichmentUnavailable",
            skipped=True,
        )
    except Exception as exc:
        return _ipure_request_result(
            transport.proxy_url,
            url,
            started,
            error=exc.__class__.__name__,
            error_type=exc.__class__.__name__,
        )


async def _get_with_retry(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float,
    started: float,
) -> httpx.Response:
    """Retry the documented 429 and transient transport resets, inside one time budget."""

    for attempt in range(IPURE_MAX_ATTEMPTS):
        remaining = timeout_seconds - (perf_counter() - started)
        if remaining <= 0:
            raise TimeoutError
        try:
            response = await transport.get(
                client,
                url,
                timeout=min(remaining, IPURE_MAX_ATTEMPT_SECONDS),
                max_bytes=IPURE_MAX_RESPONSE_BYTES,
            )
        except _IPURE_TRANSIENT_ERRORS:
            if attempt == IPURE_MAX_ATTEMPTS - 1:
                raise
            delay = _retry_delay(None, attempt)
            if delay >= timeout_seconds - (perf_counter() - started):
                raise
            await asyncio.sleep(delay)
            continue
        if response.status_code != 429 or attempt == IPURE_MAX_ATTEMPTS - 1:
            return response
        delay = _retry_delay(response, attempt)
        if delay >= timeout_seconds - (perf_counter() - started):
            return response
        await asyncio.sleep(delay)
    raise TimeoutError


async def _direct_report(
    proxy_url: str,
    url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Fetch the same report without the node egress when the node cannot reach IPure.

    IPure keys its report on the queried address, not on who asked, so this returns the
    same document the node would have received. It is evidence from a different vantage
    point and is recorded as such; nothing else in the scan ever leaves the proxy.
    """

    started = perf_counter()
    try:
        _validate_ipure_url(url)
        headers = dict(load_ipure_headers())
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(min(timeout_seconds, IPURE_DIRECT_TIMEOUT_SECONDS)),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            response = await stream_bounded_get(
                client,
                url,
                timeout=httpx.Timeout(min(timeout_seconds, IPURE_DIRECT_TIMEOUT_SECONDS)),
                max_bytes=IPURE_MAX_RESPONSE_BYTES,
                headers=headers,
            )
        if not response.is_success:
            return None
        report = _parse_ipure_report(_json_payload(response))
        if report is None:
            return None
        return _ipure_request_result(
            proxy_url,
            url,
            started,
            status_code=response.status_code,
            data=report,
            direct_fallback=True,
            budget_remaining=_budget_remaining(response),
        )
    except (httpx.HTTPError, ResponseTooLarge, ValueError, TimeoutError):
        return None


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Bounded retry wait: transient resets recover immediately, 429 keeps Retry-After."""

    delay = min(2**attempt, 2.0)
    retry_after = response.headers.get("Retry-After", "") if response is not None else ""
    if retry_after.isdigit():
        delay = max(delay, float(retry_after))
    return delay


def _response_error(
    response: httpx.Response,
    payload: Any,
) -> tuple[str, str, bool]:
    code = payload.get("code") if isinstance(payload, dict) else None
    code_suffix = f"，{code}" if isinstance(code, str) and code else ""
    if response.status_code == 403:
        if code == "verification_required":
            message = "IPure 当日免验证额度已用完，请在浏览器打开报告地址完成一次检测"
        else:
            message = f"IPure 拒绝访问（HTTP 403{code_suffix}）"
        return message, "EnrichmentUnavailable", True
    if response.status_code == 429:
        return (
            f"IPure 查询频率受限（HTTP 429{code_suffix}）",
            "EnrichmentUnavailable",
            True,
        )
    return f"HTTP {response.status_code}", "HTTPStatusError", False


def _json_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _report_url(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    return _clean_text(payload.get("reportUrl")) or None


def _budget_remaining(response: httpx.Response) -> int | None:
    value = response.headers.get("x-open-budget-remaining", "").strip()
    return int(value) if value.isdigit() else None


def _ipure_url(exit_ip: str) -> str:
    normalized = str(ipaddress.ip_address(exit_ip))
    return f"{IPURE_ORIGIN}/api/lookup?{urlencode({'ip': normalized})}"


def _parse_ipure_report(payload: Any) -> dict[str, Any] | None:
    """Normalize one /api/lookup report; the purity total is the only hard requirement.

    A restricted report may legitimately omit `risk.purity`: upstream marks that level
    as "not on the 0..100 scale". Everything here stays the raw upstream value — the
    -1 sentinel is applied in `_ipure_scores`, so `requests.ipure.data` never carries a
    rewritten number and `risk.purity`/`scenarios[].score` keep one convention.
    """

    if not isinstance(payload, dict):
        return None
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    level = _clean_text(risk.get("level")) or None
    total = _numeric_score(risk.get("purity"))
    # 受限档真的可能不带 purity；其余档位缺总分才算解析失败。
    if total is None and level != IPURE_RESTRICTED_LEVEL:
        return None

    scenarios = payload.get("scenarios") if isinstance(payload.get("scenarios"), list) else []
    by_id = {
        item.get("id"): item
        for item in scenarios
        if isinstance(item, dict) and item.get("id") in IPURE_SCENARIOS
    }
    parsed_scenarios: dict[str, dict[str, Any]] = {}
    for scenario_id in IPURE_SCENARIOS:
        item = by_id.get(scenario_id)
        if item is None:
            parsed_scenarios[scenario_id] = {"score": None, "level": None, "label": None}
            continue
        parsed_scenarios[scenario_id] = {
            "score": _numeric_score(item.get("score")),
            "level": _clean_text(item.get("level")) or None,
            "label": _clean_text(item.get("levelLabel")) or None,
        }

    scenario_applicable = payload.get("scenarioApplicable")
    stale = payload.get("stale")
    return {
        "total": total,
        "scenarios": parsed_scenarios,
        "level": level,
        "label": _clean_text(risk.get("label")) or None,
        "verdict": _clean_text(risk.get("verdict")) or None,
        "confidence": _clean_text(risk.get("confidence")) or None,
        "source": _clean_text(payload.get("source")) or None,
        "stale": stale if isinstance(stale, bool) else None,
        "queried_at": _clean_text(payload.get("queriedAt")) or None,
        "scenario_applicable": (
            scenario_applicable if isinstance(scenario_applicable, bool) else None
        ),
        "scenario_note": _clean_text(payload.get("scenarioNote")) or None,
        "report_url": _clean_text(payload.get("reportUrl")) or None,
        "network": _parse_ipure_network(payload),
    }


# 报告里关于这个 IP 本身的描述（2026-10 实测 8.8.8.8 的 /api/lookup 响应）：
#   ip                                  回显的被查 IP
#   geo.country / geo.countryName       国家代码（"US"）/ 国名（"United States"）；region / city
#   asn.asn / asn.name / asn.org        AS 号 / AS 名（"GOOGLE - Google LLC, US"）/
#                                       组织（"Google Public DNS"）
#   registry.org / registry.country     RIR 登记的持有者与登记国
#   usageType                           residential · mobile · business · hosting · education ·
#                                       government · unknown（官方 /docs/api 枚举）
#   nativeType                          native · broadcast（注册地与使用地不一致）· unknown
# 字段缺失或类型不对一律记 None，由 collector 回落到 Coffee。
_AS_NAME = re.compile(r"^\S+\s+-\s+(.+?)(?:,\s*[A-Z]{2})?$")
# 枚举外的取值与 unknown 一律当没说，由 Coffee 补。
IPURE_USAGE_TYPES = ("residential", "mobile", "business", "hosting", "education", "government")
_NATIVE_TYPES = {"native": True, "broadcast": False}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> str | None:
    return _clean_text(value) or None if isinstance(value, str) else None


def _as_name(value: Any) -> str | None:
    """ "GOOGLE - Google LLC, US" → "Google LLC"；不是这个形状就原样返回（去掉末尾国家代码）。"""

    text = _text_or_none(value)
    if not text:
        return None
    match = _AS_NAME.match(text)
    if match:
        return match.group(1).strip() or text
    return re.sub(r",\s*[A-Z]{2}$", "", text) or text


def _parse_ipure_network(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull the exit IP's identity out of one /api/lookup report; nothing is guessed."""

    geo = _dict(payload.get("geo"))
    asn_info = _dict(payload.get("asn"))
    registry = _dict(payload.get("registry"))

    ip_text = _text_or_none(payload.get("ip"))
    try:
        ip = str(ipaddress.ip_address(ip_text)) if ip_text else None
    except ValueError:
        ip = None

    code = _text_or_none(geo.get("country"))
    country_code = code.upper() if code and re.fullmatch(r"[A-Za-z]{2}", code) else None

    asn = asn_info.get("asn")
    asn = (
        asn
        if isinstance(asn, int) and not isinstance(asn, bool) and 0 < asn <= 4_294_967_295
        else None
    )

    usage = (_text_or_none(payload.get("usageType")) or "").lower()
    usage = usage if usage in IPURE_USAGE_TYPES else ""
    native = (_text_or_none(payload.get("nativeType")) or "").lower()
    native = native if native in _NATIVE_TYPES else ""
    registered = _text_or_none(registry.get("country"))

    return {
        "ip": ip,
        "country": _text_or_none(geo.get("countryName")),
        "country_code": country_code,
        "region": _text_or_none(geo.get("region")),
        "city": _text_or_none(geo.get("city")),
        "asn": asn,
        "as_name": _as_name(asn_info.get("name")),
        "org": _text_or_none(asn_info.get("org")),
        "registry_org": _text_or_none(registry.get("org")),
        "registered_country": registered.upper() if registered else None,
        "usage_type": usage or None,
        # 「住宅」是 usageType 里的一档：residential 为 true，其余已知档位（含 mobile）为 false。
        "is_residential": (usage == "residential") if usage else None,
        "native_type": native or None,
        "is_native": _NATIVE_TYPES.get(native),
    }


def _ipure_score(value: Any, level: Any) -> int | None:
    """Map one upstream score to the record's value, restricted → -1 sentinel."""

    if _clean_text(level) == IPURE_RESTRICTED_LEVEL:
        return IPURE_RESTRICTED_SCORE
    return _numeric_score(value)


def _ipure_scores(result: dict[str, Any]) -> dict[str, int | None]:
    """Flatten a report into the node record's score map; every key is always present."""

    data = result.get("data") if isinstance(result.get("data"), dict) else None
    scores: dict[str, int | None] = {key: None for key in ("total", *IPURE_SCENARIOS)}
    if data is None:
        return scores
    scores["total"] = _ipure_score(data.get("total"), data.get("level"))
    scenarios = data.get("scenarios") if isinstance(data.get("scenarios"), dict) else {}
    for scenario_id in IPURE_SCENARIOS:
        item = scenarios.get(scenario_id)
        if isinstance(item, dict):
            scores[scenario_id] = _ipure_score(item.get("score"), item.get("level"))
    return scores


def _ipure_field(result: dict[str, Any], key: str) -> Any:
    data = result.get("data") if isinstance(result.get("data"), dict) else None
    return data.get(key) if isinstance(data, dict) else None


def _ipure_request_result(
    proxy_url: str,
    url: str,
    started: float,
    *,
    status_code: int | None = None,
    data: dict[str, Any] | None = None,
    error: str | None = None,
    error_type: str | None = None,
    location: str | None = None,
    skipped: bool = False,
    report_url: str | None = None,
    budget_remaining: int | None = None,
    direct_fallback: bool = False,
) -> dict[str, Any]:
    return {
        "url": url,
        "attempted": True,
        "via_mihomo": not direct_fallback,
        "proxy_url": proxy_url,
        "target_host": IPURE_HOST,
        "ok": status_code is not None and 200 <= status_code < 300 and error is None,
        "status_code": status_code,
        "elapsed_ms": round((perf_counter() - started) * 1000),
        "data": data,
        "error": error,
        "error_type": error_type,
        "location": location,
        "skipped": skipped,
        "report_url": report_url or (data.get("report_url") if isinstance(data, dict) else None),
        "budget_remaining": budget_remaining,
        "direct_fallback": direct_fallback,
    }
