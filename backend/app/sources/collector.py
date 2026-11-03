from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import quote

from . import coffee, gpt_checks
from . import ipqs as ipqs_source
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
    IPQS_HOST,
    IPQS_TIMEOUT_SECONDS,
    IPURE_HOST,
    IPURE_ORIGIN,
    IPURE_TIMEOUT_SECONDS,
    ProxyTransport,
)
from .ipure import _ipure_field, _ipure_scores, _ipure_url


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
    ) -> dict[str, Any]:
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
                ipqs_task = asyncio.create_task(
                    ipqs_source.request(
                        self.transport,
                        client,
                        ipqs_source.ipqs_url(exit_ip),
                        timeout_seconds=self.transport.deadline(IPQS_TIMEOUT_SECONDS),
                    )
                )
                all_request_tasks = [
                    gpt_task, lookup_task, *optional_tasks, ipure_task, ipqs_task
                ]
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
                    ipqs = await ipqs_task
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
                ipqs = _missing_result("", reason, skipped=True)

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
                "ipqs": ipqs,
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
            page, trace, lookup, global_ping, port_scan, ping_check, related, ipure, ipqs
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
            "ipqs": ipqs,
        }
        completeness = _completeness(request_results, exit_ip, lookup_data)
        gpt_check = _gpt_check_summary(gpt_requests, lookup_data)
        coffee_summary = _profile_summary(lookup_data)
        ipure_scores = _ipure_scores(ipure)
        summary = {
            **coffee_summary,
            "coffee_score": coffee_summary["score"],
            "score": ipure_scores["total"],
            "ipure_scores": ipure_scores,
            "ipure_report_url": _ipure_field(ipure, "report_url"),
            "gpt_check": gpt_check,
            **ipqs_source.ipqs_fields(ipqs),
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
                "ipqs_via_direct_fallback": bool(ipqs.get("direct_fallback")),
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
        "ipqs_recorded": _request_recorded(requests["ipqs"]),
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
        and (not result.get("skipped") or result.get("target_host") in {IPURE_HOST, IPQS_HOST})
    ]
    return "；".join(dict.fromkeys(errors)) or None


def _now() -> str:
    return datetime.now(UTC).isoformat()
