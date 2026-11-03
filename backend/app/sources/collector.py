from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import quote

from . import coffee, gpt_checks
from . import ipure as ipure_source
from .coffee import (
    _global_ping_summary,
    _global_ping_url,
    _is_same_ip,
    _missing_result,
    _ping_check_summary,
    _port_scan_summary,
    _profile_summary,
    _related_domains,
    _trace_ip,
    _without_data,
)
from .gpt_checks import _gpt_check_summary
from .http import (
    COFFEE_ORIGIN,
    COFFEE_PAGE_URL,
    COFFEE_TRACE_URL,
    IPURE_HOST,
    IPURE_ORIGIN,
    IPURE_TIMEOUT_SECONDS,
    ProxyTransport,
)
from .ipure import _ipure_field, _ipure_scores, _ipure_url
from .values import _clean_text


class CoffeeCollector:
    """Coordinate dependent source requests and assemble record schema v1."""

    def __init__(self, proxy_url: str, *, timeout_ms: int) -> None:
        self.transport = ProxyTransport(proxy_url, timeout_ms=timeout_ms)

    async def collect(
        self,
        *,
        job_id: str,
        node_index: int,
        node_name: str,
        node_type: str,
        selected_proxy: str,
        mihomo_instance: str,
        on_exit_ip: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """on_exit_ip：trace 取到出口 IP、开始查 IP 资料的那一刻回调一次（只给实时进度用）。"""
        started_at = _now()
        started = perf_counter()
        async with self.transport.client() as client:
            page, trace = await asyncio.gather(
                coffee.request(
                    self.transport,
                    client,
                    COFFEE_PAGE_URL,
                    payload="html",
                    timeout_seconds=self.transport.deadline(12),
                ),
                coffee.request(
                    self.transport,
                    client,
                    COFFEE_TRACE_URL,
                    payload="text",
                    timeout_seconds=self.transport.deadline(8),
                ),
            )
            exit_ip = _trace_ip(trace.get("data"))
            if exit_ip and on_exit_ip is not None:
                on_exit_ip()
            gpt_requests: dict[str, dict[str, Any]] = {}

            lookup_url = (
                f"{COFFEE_ORIGIN}/api/ip/lookup/{quote(exit_ip, safe='')}" if exit_ip else ""
            )
            if exit_ip:
                gpt_task = asyncio.create_task(gpt_checks.requests(self.transport, client))
                lookup_task = asyncio.create_task(
                    coffee.request(
                        self.transport,
                        client,
                        lookup_url,
                        payload="json",
                        timeout_seconds=self.transport.deadline(45),
                    )
                )
                optional_tasks = [
                    asyncio.create_task(
                        coffee.request(
                            self.transport,
                            client,
                            _global_ping_url(exit_ip),
                            payload="json",
                            timeout_seconds=self.transport.deadline(8),
                        )
                    ),
                    asyncio.create_task(
                        coffee.request(
                            self.transport,
                            client,
                            f"{COFFEE_ORIGIN}/api/ip/portscan/{exit_ip}?probe=0",
                            payload="json",
                            timeout_seconds=self.transport.deadline(8),
                        )
                    ),
                    asyncio.create_task(
                        coffee.request(
                            self.transport,
                            client,
                            f"{COFFEE_ORIGIN}/api/ip/pingcheck/{exit_ip}",
                            payload="json",
                            timeout_seconds=self.transport.deadline(8),
                        )
                    ),
                ]
                ipure_task = asyncio.create_task(
                    ipure_source.request(
                        self.transport,
                        client,
                        _ipure_url(exit_ip),
                        timeout_seconds=self.transport.deadline(IPURE_TIMEOUT_SECONDS),
                    )
                )
                all_request_tasks = [gpt_task, lookup_task, *optional_tasks, ipure_task]
                try:
                    lookup = await lookup_task
                    lookup_data = lookup.get("data") if isinstance(lookup.get("data"), dict) else {}
                    if lookup.get("ok") and not _is_same_ip(lookup_data.get("ip"), exit_ip):
                        lookup = {
                            **lookup,
                            "ok": False,
                            "error": "IP lookup 未返回与 Coffee trace 一致的 IP 数据",
                        }
                        lookup_data = {}
                    related = await coffee.related_result(
                        self.transport, client, exit_ip, lookup_data
                    )
                    global_ping, port_scan, ping_check = await asyncio.gather(*optional_tasks)
                    gpt_requests = await gpt_task
                    ipure = await ipure_task
                finally:
                    for task in all_request_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*all_request_tasks, return_exceptions=True)
            else:
                lookup = _missing_result("", "Coffee trace 未返回出口 IP", skipped=True)
                lookup_data = {}
                reason = "未取得出口 IP"
                global_ping = _missing_result("", reason, skipped=True)
                port_scan = _missing_result("", reason, skipped=True)
                ping_check = _missing_result("", reason, skipped=True)
                related = _missing_result("", reason, skipped=True)
                ipure = _missing_result("", reason, skipped=True)

        status = _node_status(
            page,
            trace,
            lookup,
            exit_ip,
            {
                "global_ping": global_ping,
                "port_scan": port_scan,
                "ping_check": ping_check,
                "ipure": ipure,
                **(
                    {"related": related}
                    if lookup_data.get("related_pending")
                    or lookup_data.get("related_domains_pending")
                    else {}
                ),
            },
        )
        final_exit_ip = exit_ip if status != "failed" else None
        error = _combined_error(
            page, trace, lookup, global_ping, port_scan, ping_check, related, ipure
        )
        request_results = {
            "page": page,
            "trace": trace,
            "lookup": lookup,
            "global_ping": global_ping,
            "port_scan": port_scan,
            "ping_check": ping_check,
            "related": related,
            "ipure": ipure,
        }
        completeness = _completeness(request_results, exit_ip, lookup_data)
        gpt_check = _gpt_check_summary(gpt_requests, lookup_data)
        coffee_summary = _profile_summary(lookup_data)
        ipure_scores = _ipure_scores(ipure)
        identity = _network_identity(
            exit_ip if status != "failed" else "",
            _ipure_field(ipure, "network"),
            lookup_data,
            coffee_summary,
        )
        summary = {
            **coffee_summary,
            **identity,
            "coffee_score": coffee_summary["score"],
            "score": ipure_scores["total"],
            "ipure_scores": ipure_scores,
            "ipure_report_url": _ipure_field(ipure, "report_url"),
            "gpt_check": gpt_check,
        }
        finished_at = _now()

        return {
            "schema_version": 1,
            "job_id": job_id,
            "node_index": node_index,
            "node": node_name,
            "type": node_type,
            "selected_proxy": selected_proxy,
            "coffee_page_url": COFFEE_PAGE_URL,
            "status": status,
            "error": error,
            "transport_error": None,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_ip": final_exit_ip,
            **summary,
            "global_ping": _global_ping_summary(global_ping),
            "port_scan": _port_scan_summary(port_scan),
            "ping_check": _ping_check_summary(ping_check),
            "related_domains": _related_domains(lookup_data, related),
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "proxy_evidence": {
                "transport": "workspace_mihomo_mixed_port",
                "proxy_url": self.transport.proxy_url,
                "mihomo_instance": mihomo_instance,
                "selector": "BEST-IP",
                "selected_proxy": selected_proxy,
                "selection_confirmed": selected_proxy == node_name,
                "target_origin": COFFEE_ORIGIN,
                "enrichment_origin": IPURE_ORIGIN,
                "trust_env": False,
                "ipure_budget_remaining": ipure.get("budget_remaining"),
                "ipure_via_direct_fallback": bool(ipure.get("direct_fallback")),
            },
            "completeness": completeness,
            "requests": {
                **request_results,
                "gpt_check": gpt_check,
            },
            "coffee": {
                "page": page.get("data"),
                "trace": trace.get("data"),
                "lookup": lookup.get("data"),
                "global_ping": global_ping.get("data"),
                "port_scan": port_scan.get("data"),
                "ping_check": ping_check.get("data"),
                "related": related.get("data"),
                "gpt_check": gpt_check,
            },
            "pages": {
                "ip": {
                    "name": "ip",
                    "url": COFFEE_PAGE_URL,
                    "status": status,
                    "exit_ip": final_exit_ip,
                    "score": summary["coffee_score"],
                    "page_request": _without_data(page),
                    "trace": trace,
                    "lookup_request": _without_data(lookup),
                    "result": lookup_data or None,
                    "error": error,
                },
            },
        }


def _network_identity(
    exit_ip: str,
    ipure_network: Any,
    lookup_data: dict[str, Any],
    coffee_summary: dict[str, Any],
) -> dict[str, Any]:
    """出口 IP / 国家和地区 / 服务商 / 接入 / 原生性：IPure 报告优先，它没说的那一组才回落 Coffee。

    出口 IP 仍由 trace 发现（IPure 的查询必须带上一个 IP），IPure 回显的 ip 与之一致时记为
    「由 IPure 确认」。每一组各自记来源（network_source），页面与审计都能看出这一格是谁说的。
    """

    network = ipure_network if isinstance(ipure_network, dict) else {}
    identity: dict[str, Any] = {}
    source: dict[str, str | None] = {}

    ipure_ip = network.get("ip")
    source["exit_ip"] = (
        ("ipure" if ipure_ip and _is_same_ip(ipure_ip, exit_ip) else "coffee") if exit_ip else None
    )

    # 地区组：国名或国家代码任一在，就整组用 IPure。
    coffee_code = _clean_text(lookup_data.get("countryCode") or lookup_data.get("country_code"))
    if network.get("country") or network.get("country_code"):
        source["geo"] = "ipure"
        geo = {
            "country": network.get("country") or _clean_text(lookup_data.get("country")) or None,
            "country_code": network.get("country_code") or coffee_code.upper() or None,
            "region": network.get("region"),
            "city": network.get("city"),
        }
    else:
        geo = {
            "country": _clean_text(lookup_data.get("country")) or None,
            "country_code": coffee_code.upper() or None,
            "region": _clean_text(lookup_data.get("region")) or None,
            "city": _clean_text(lookup_data.get("city")) or None,
        }
        source["geo"] = "coffee" if geo["country"] or geo["country_code"] else None
    identity.update(geo)
    identity["location"] = (
        " ".join(part for part in (geo["country"], geo["region"], geo["city"]) if part)
        if source["geo"] == "ipure"
        else coffee_summary.get("location", "")
    )

    # 服务商组：ISP 显示 asn.org（最贴近「谁在运营这段地址」），AS 组织显示清洗过的 AS 名。
    ipure_isp = network.get("org") or network.get("as_name") or network.get("registry_org")
    if ipure_isp or network.get("asn"):
        source["isp"] = "ipure"
        identity["isp"] = ipure_isp or coffee_summary.get("isp") or ""
        identity["as_org"] = (
            network.get("as_name") or network.get("registry_org") or ipure_isp or ""
        )
        identity["asn"] = network.get("asn") or coffee_summary.get("asn")
    else:
        source["isp"] = "coffee" if coffee_summary.get("isp") or coffee_summary.get("asn") else None

    # 接入：IPure 的 usageType 六档（住宅 / 移动 / 商业 / 机房 / 教育 / 政府）；
    # unknown 才回落 Coffee。
    if network.get("usage_type"):
        source["kind"] = "ipure"
        identity["usage_type"] = network["usage_type"]
        identity["is_residential"] = network["is_residential"]
        identity["is_datacenter"] = network["usage_type"] == "hosting"
    else:
        source["kind"] = (
            "coffee" if isinstance(coffee_summary.get("is_residential"), bool) else None
        )

    # 原生性：nativeType 同理；广播时用 IPure 的登记国补出「广播 IP (XX)」。
    if isinstance(network.get("is_native"), bool):
        source["native"] = "ipure"
        identity["is_native"] = network["is_native"]
        registered = network.get("registered_country")
        if network["is_native"]:
            identity["native_status"] = "原生 IP"
            identity["native_detail"] = ""
        else:
            identity["native_status"] = f"广播 IP ({registered})" if registered else "广播 IP"
            identity["native_detail"] = (
                f"IP注册在 {registered} 和IP归属地 {geo['country_code'] or ''} 不一致".strip()
                if registered
                else ""
            )
    else:
        source["native"] = "coffee" if isinstance(coffee_summary.get("is_native"), bool) else None

    identity["network_source"] = source
    return identity


def _node_status(
    page: dict[str, Any],
    trace: dict[str, Any],
    lookup: dict[str, Any],
    exit_ip: str,
    optional: dict[str, dict[str, Any]] | None = None,
) -> str:
    required_ok = bool(page.get("ok") and trace.get("ok") and lookup.get("ok") and exit_ip)
    if not required_ok:
        return "failed"
    if optional and any(not _request_recorded(item) for item in optional.values()):
        return "partial"
    return "success"


def _completeness(
    requests: dict[str, dict[str, Any]],
    exit_ip: str,
    lookup_data: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "page_received": bool(requests["page"].get("ok")),
        "trace_received": bool(requests["trace"].get("ok")),
        "exit_ip_valid": bool(exit_ip),
        "lookup_received": bool(requests["lookup"].get("ok")),
        "lookup_matches_trace": bool(exit_ip and _is_same_ip(lookup_data.get("ip"), exit_ip)),
        "global_ping_recorded": _request_recorded(requests["global_ping"]),
        "port_scan_recorded": _request_recorded(requests["port_scan"]),
        "ping_check_recorded": _request_recorded(requests["ping_check"]),
        "ipure_recorded": _request_recorded(requests["ipure"]),
        "related_recorded": (
            _request_recorded(requests["related"])
            if lookup_data.get("related_pending") or lookup_data.get("related_domains_pending")
            else True
        ),
    }
    required = (
        "page_received",
        "trace_received",
        "exit_ip_valid",
        "lookup_received",
        "lookup_matches_trace",
    )
    missing = [name for name in required if not checks[name]]
    unrecorded = [
        name for name, value in checks.items() if name.endswith("_recorded") and not value
    ]
    return {
        "complete": not missing and not unrecorded,
        "checks": checks,
        "missing_required": missing,
        "unrecorded_requests": unrecorded,
    }


def _request_recorded(result: dict[str, Any]) -> bool:
    return bool(
        result.get("attempted") and result.get("status_code") is not None and result.get("ok")
    )


def _combined_error(*results: dict[str, Any]) -> str | None:
    errors = [
        result.get("error")
        for result in results
        if result.get("error")
        and (not result.get("skipped") or result.get("target_host") == IPURE_HOST)
    ]
    return "；".join(dict.fromkeys(errors)) or None


def _now() -> str:
    return datetime.now(UTC).isoformat()
