from __future__ import annotations

import ipaddress
import re
from math import isfinite
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx

from .http import (
    COFFEE_HOST,
    COFFEE_ORIGIN,
    GLOBAL_PING_NODES,
    ProxyTransport,
    _validate_coffee_url,
)
from .values import _clean_text, _is_finite_number, _numeric_score


async def request(
    transport: ProxyTransport,
    client: httpx.AsyncClient,
    url: str,
    *,
    payload: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    _validate_coffee_url(url)
    started = perf_counter()
    try:
        response = await transport.get(client, url, timeout=timeout_seconds)
        if 300 <= response.status_code < 400:
            return {
                "url": url,
                "attempted": True,
                "via_mihomo": True,
                "proxy_url": transport.proxy_url,
                "target_host": COFFEE_HOST,
                "ok": False,
                "status_code": response.status_code,
                "elapsed_ms": round((perf_counter() - started) * 1000),
                "data": None,
                "error": "redirect_rejected",
                "error_type": "RedirectRejected",
                "location": response.headers.get("location"),
            }
        data, parse_error = parse_response(response, payload)

        ok = response.is_success and parse_error is None
        if parse_error:
            error = parse_error
        elif not response.is_success:
            error = f"HTTP {response.status_code}"
        else:
            error = None
        return {
            "url": url,
            "attempted": True,
            "via_mihomo": True,
            "proxy_url": transport.proxy_url,
            "target_host": COFFEE_HOST,
            "ok": ok,
            "status_code": response.status_code,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "data": data,
            "error": error,
            "error_type": "ResponseParseError" if parse_error else None,
            "location": response.headers.get("location"),
        }
    except Exception as exc:
        return {
            "url": url,
            "attempted": True,
            "via_mihomo": True,
            "proxy_url": transport.proxy_url,
            "target_host": COFFEE_HOST,
            "ok": False,
            "status_code": None,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "data": None,
            "error": exc.__class__.__name__,
            "error_type": exc.__class__.__name__,
            "location": None,
        }


def parse_response(response: httpx.Response, payload: str) -> tuple[Any, str | None]:
    data: Any = None
    parse_error: str | None = None
    if payload == "json":
        try:
            data = response.json()
        except ValueError:
            parse_error = "响应不是有效 JSON"
    elif payload == "text":
        data = response.text[:20000]
    else:
        raise ValueError(f"不支持的响应类型：{payload}")

    return data, parse_error


def _global_ping_url(exit_ip: str) -> str:
    params = [("host", exit_ip), *(("node", item["node"]) for item in GLOBAL_PING_NODES)]
    return f"{COFFEE_ORIGIN}/api/ping/global?{urlencode(params)}"


def _is_same_ip(value: Any, expected: str) -> bool:
    try:
        return ipaddress.ip_address(value) == ipaddress.ip_address(expected)
    except (TypeError, ValueError):
        return False


def _trace_ip(data: Any) -> str:
    value = _trace_value(data, "ip")
    try:
        return str(ipaddress.ip_address(value))
    except (TypeError, ValueError):
        return ""


def _trace_value(data: Any, key: str) -> str:
    if not isinstance(data, str):
        return ""
    prefix = f"{key}="
    for line in data.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _format_native(lookup_data: dict[str, Any]) -> dict[str, Any]:
    # 对标 Coffee IP 原生性逻辑:
    # 比较 countryCode 与 registered_country_code
    cc_lo = _clean_text(lookup_data.get("countryCode") or lookup_data.get("country_code")).lower()
    reg_lo = _clean_text(
        lookup_data.get("registered_country_code") or lookup_data.get("registeredCountryCode")
    ).lower()
    reg_name = _clean_text(
        lookup_data.get("registered_country") or lookup_data.get("registeredCountry")
    )
    is_public_service = bool(lookup_data.get("is_public_service"))

    if is_public_service:
        return {"is_native": None, "native_status": "任播服务", "native_detail": ""}

    if not cc_lo or not reg_lo:
        return {"is_native": None, "native_status": "未知", "native_detail": ""}

    if cc_lo == reg_lo:
        return {"is_native": True, "native_status": "原生 IP", "native_detail": ""}

    reg_up = reg_lo.upper()
    country_up = _clean_text(lookup_data.get("country")).upper()
    tip_more = f" ({reg_name})" if reg_name else ""
    return {
        "is_native": False,
        "native_status": f"广播 IP ({reg_up})",
        "native_detail": f"IP注册在 {reg_up}{tip_more} 和IP归属地 {country_up} 不一致",
    }


def _abuser_level_label(raw_level: Any, raw_score: Any) -> dict[str, str]:
    # 对标 Coffee IP abuserLevelLabel:
    # >= 0.25 极高风险, >= 0.10 高风险, >= 0.05 中风险, > 0.005 低风险, 0 纯净/极度纯净
    n = None
    if raw_score is not None:
        try:
            m = re.search(r"([0-9]*\.?[0-9]+)", str(raw_score))
            if m:
                n = float(m.group(1))
        except (ValueError, TypeError):
            n = None

    raw_str = str(raw_level or "").lower()
    if n is not None and isfinite(n):
        if n > 0.25:
            return {"label": "极高风险", "risk": "bad"}
        if n >= 0.10:
            return {"label": "高风险", "risk": "bad"}
        if n >= 0.05:
            return {"label": "中风险", "risk": "warn"}
        if n > 0.005:
            return {"label": "低风险", "risk": "warn"}
        return {"label": "纯净", "risk": "ok"}

    if "very_high" in raw_str:
        return {"label": "极高风险", "risk": "bad"}
    if "high" in raw_str:
        return {"label": "高风险", "risk": "bad"}
    if "med" in raw_str:
        return {"label": "中风险", "risk": "warn"}
    if "low" in raw_str:
        return {"label": "低风险", "risk": "warn"}
    if "safe" in raw_str:
        return {"label": "纯净", "risk": "ok"}
    return {"label": "纯净", "risk": "ok"}


def _httpbl_level_label(raw_threat: Any) -> dict[str, str]:
    # 对标 Coffee IP HTTP 蜜罐威胁等级
    if raw_threat is None or raw_threat == "":
        return {"label": "纯净", "risk": "ok"}
    try:
        val = int(raw_threat)
        if val <= 0:
            return {"label": "纯净", "risk": "ok"}
        if val >= 5:
            return {"label": "高风险", "risk": "bad"}
        if val >= 2:
            return {"label": "中风险", "risk": "warn"}
        return {"label": "低风险", "risk": "warn"}
    except (ValueError, TypeError):
        return {"label": "纯净", "risk": "ok"}


def _asn_kind_label(asn_kind: Any) -> str:
    kind_map = {
        "hosting": "机房/托管",
        "mobile": "移动网络",
        "residential": "住宅宽带",
        "backbone": "骨干网",
        "isp": "运营商",
        "cdn": "CDN 内容分发",
        "business": "商业专线",
        "mixed": "混合",
        "unknown": "未知",
    }
    k = str(asn_kind or "").lower()
    return kind_map.get(k, k or "未知")


def _profile_summary(ip_result: dict[str, Any]) -> dict[str, Any]:
    residential_value = ip_result.get("isResidential")
    if residential_value is None:
        residential_value = ip_result.get("is_residential")
    datacenter_value = ip_result.get("is_datacenter")
    if datacenter_value is None:
        datacenter_value = ip_result.get("isDatacenter")
    is_residential = residential_value if isinstance(residential_value, bool) else None
    is_datacenter = datacenter_value if isinstance(datacenter_value, bool) else None

    # 原生性解析
    native_info = _format_native(ip_result)
    is_native = native_info["is_native"]
    native_status = native_info["native_status"]
    native_detail = native_info["native_detail"]

    # Bogon / 广播
    is_bogon = bool(ip_result.get("is_bogon"))
    bogon_reason = _clean_text(ip_result.get("bogon_reason"))

    # RPKI 状态
    rpki_raw = _clean_text(ip_result.get("rpki_status")).lower()
    if rpki_raw == "valid":
        rpki_status = "✓ Valid"
    elif rpki_raw == "invalid":
        rpki_status = "✗ Invalid"
    elif rpki_raw:
        rpki_status = rpki_raw
    else:
        rpki_status = "未知"

    # ASN 及自报类型
    asn_kind_raw = _clean_text(ip_result.get("asn_kind"))
    asn_kind_display = _asn_kind_label(asn_kind_raw)

    # 人机流量画像
    is_crawler = (
        ip_result.get("is_crawler") if isinstance(ip_result.get("is_crawler"), bool) else None
    )
    is_abuser = ip_result.get("is_abuser") if isinstance(ip_result.get("is_abuser"), bool) else None
    is_public_service = bool(ip_result.get("is_public_service"))
    if is_public_service:
        traffic_profile = "服务器/任播 DNS"
    elif is_crawler:
        traffic_profile = "偏爬虫"
    elif is_datacenter:
        traffic_profile = "机器偏多"
    elif is_residential or is_datacenter is False:
        traffic_profile = "人类偏多"
    elif is_crawler or is_abuser:
        traffic_profile = "人机混合 / 爬虫偏多"
    else:
        traffic_profile = "未知"

    # 运营商类型 (company_type)
    company_type_value = _clean_text(ip_result.get("company_type"))
    if company_type_value.lower() == "isp" or is_residential:
        company_type = "ISP（家庭宽带）"
    elif is_datacenter or company_type_value.lower() == "hosting":
        company_type = "Hosting"
    elif company_type_value:
        company_type = company_type_value
    else:
        company_type = "未知"

    # 风险深度检测 (VPN / Proxy / Tor / Crawler / Abuser)
    risk_keys = ("is_vpn", "is_proxy", "is_tor", "is_crawler", "is_abuser")
    risk_values = {
        key: ip_result.get(key) if isinstance(ip_result.get(key), bool) else None
        for key in risk_keys
    }
    risk_labels = {
        "is_vpn": "VPN",
        "is_proxy": "代理 (Proxy)",
        "is_tor": "Tor",
        "is_crawler": "爬虫/机器人",
        "is_abuser": "历史滥用",
    }
    risk_flags = [risk_labels[key] for key, value in risk_values.items() if value]

    # IP 情报 / 威胁指标
    intel = ip_result.get("intelligence") if isinstance(ip_result.get("intelligence"), dict) else {}
    threats = intel.get("threats") if isinstance(intel.get("threats"), list) else []
    threat_labels = [
        _clean_text(threat.get("label"))
        for threat in threats
        if isinstance(threat, dict) and threat.get("label")
    ]
    abuse_info = _abuser_level_label(intel.get("abuser_level"), intel.get("abuser_score_raw"))
    rep_threat = (
        intel.get("rep_threat")
        if intel.get("rep_threat") is not None
        else intel.get("httpbl_threat")
    )
    honeypot_info = _httpbl_level_label(rep_threat)

    if risk_flags or threat_labels:
        all_flags = list(dict.fromkeys(risk_flags + threat_labels))
        security_status = "⚠️ " + " · ".join(all_flags)
    elif all(value is not None for value in risk_values.values()):
        security_status = "🛡️ 纯净 (未发现明显威胁)"
    elif ip_result:
        security_status = "检测数据不足"
    else:
        security_status = "检测失败"

    ai_verdict = (
        ip_result.get("ai_verdict") if isinstance(ip_result.get("ai_verdict"), dict) else {}
    )

    return {
        "cidr": _clean_text(ip_result.get("cidr")),
        "rdns": _clean_text(ip_result.get("rdns"), default="-"),
        "ai_verdict": _clean_text(ai_verdict.get("label")),
        "location": _location(ip_result),
        "isp": _clean_text(
            ip_result.get("isp") or ip_result.get("asOrganization") or ip_result.get("as_org")
        ),
        "score": _numeric_score(ip_result.get("trust_score")),
        "is_residential": is_residential,
        "is_datacenter": is_datacenter,
        "is_native": is_native,
        "native_status": native_status,
        "native_detail": native_detail,
        "is_bogon": is_bogon,
        "bogon_status": "是" if is_bogon else "否（公网可达）",
        "bogon_reason": bogon_reason,
        "rpki_status": rpki_status,
        "asn_kind": asn_kind_raw,
        "asn_kind_display": asn_kind_display,
        "abuse_level": abuse_info["label"],
        "honeypot_status": honeypot_info["label"],
        "traffic_profile": traffic_profile,
        "company_type": company_type,
        **risk_values,
        "security_status": security_status,
        "threat_tags": threat_labels,
        "asn": _normalize_asn(ip_result.get("asn")),
        "as_org": _clean_text(
            ip_result.get("asOrganization") or ip_result.get("as_org") or ip_result.get("isp")
        ),
    }


def _global_ping_summary(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    latencies = data.get("results") if isinstance(data.get("results"), dict) else {}
    timeout_values = data.get("timeouts")
    pending_values = data.get("pending")
    timeouts = (
        {item for item in timeout_values if isinstance(item, str)}
        if isinstance(timeout_values, list)
        else set()
    )
    pending = (
        {item for item in pending_values if isinstance(item, str)}
        if isinstance(pending_values, list)
        else set()
    )
    request_error = data.get("error") or response.get("error")

    results = []
    for target in GLOBAL_PING_NODES:
        latency = latencies.get(target["node"])
        ok = _is_finite_number(latency) and latency >= 0
        elapsed_ms = round(latency) if ok else -1
        if ok:
            status = f"{elapsed_ms} ms"
        elif target["node"] in timeouts:
            status = "超时"
        elif target["node"] in pending:
            status = "等待"
        elif response.get("attempted"):
            status = "未返回"
        else:
            status = "未检测"
        results.append(
            {
                "code": target["code"],
                "name": target["name"],
                "node": target["node"],
                "ok": ok,
                "elapsed_ms": elapsed_ms,
                "status": status,
                "error": str(request_error) if request_error else None,
            }
        )
    return results


def _port_scan_summary(response: dict[str, Any]) -> Any:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    return data.get("ports")


def _ping_check_summary(response: dict[str, Any]) -> dict[str, Any] | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if not data:
        return None
    ok_nodes = data.get("ok_nodes", 0)
    total_nodes = data.get("total_nodes", 0)
    ok_ratio = (
        ok_nodes / total_nodes
        if _is_finite_number(ok_nodes) and _is_finite_number(total_nodes) and total_nodes > 0
        else 0.0
    )
    return {
        "verdict": data.get("verdict", "unknown"),
        "reachable": data.get("reachable", False),
        "ok_nodes": ok_nodes,
        "total_nodes": total_nodes,
        "ok_ratio": ok_ratio,
    }


def _related_domains(lookup_data: dict[str, Any]) -> list[Any]:
    values = lookup_data.get("related_domains")
    return values if isinstance(values, list) else []


def _location(geo: dict[str, Any]) -> str:
    return " ".join(
        _clean_text(geo.get(key))
        for key in ("country", "region", "city")
        if _clean_text(geo.get(key))
    )


def _normalize_asn(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        normalized = re.sub(r"^(?:AS\s*)+", "", _clean_text(value), flags=re.IGNORECASE)
        if not normalized.isdigit():
            return None
        number = int(normalized)
    return number if 0 < number <= 4_294_967_295 else None


def _missing_result(url: str, message: str, *, skipped: bool = False) -> dict[str, Any]:
    return {
        "url": url,
        "attempted": False,
        "via_mihomo": False,
        "proxy_url": None,
        "target_host": COFFEE_HOST if url else None,
        "ok": False,
        "status_code": None,
        "elapsed_ms": 0,
        "data": None,
        "error": message,
        "error_type": None,
        "location": None,
        "skipped": skipped,
    }
