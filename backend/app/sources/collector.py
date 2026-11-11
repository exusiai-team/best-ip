from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from urllib.parse import quote

import httpx

from . import coffee, gpt_checks
from . import ipqs as ipqs_source
from . import ipure as ipure_source
from . import ipx as ipx_source
from . import who as who_source
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
    IPX_HOST,
    IPX_SELF_URL,
    IPX_TIMEOUT_SECONDS,
    WHO_HOST,
    WHO_ME_URL,
    WHO_ORIGIN,
    WHO_TIMEOUT_SECONDS,
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
        """on_exit_ip：trace 确认出口 IP、开始等 IP 资料的那一刻回调一次（只给实时进度用）。"""
        started_at = _now()
        started = perf_counter()
        # 不需要出口 IP 的请求（页面、trace、who、ipx /json、GPT 探测）在第 0 秒一起发出。
        async with (
            self.transport.client() as client,
            _background(
                coffee.request(
                    self.transport,
                    client,
                    COFFEE_PAGE_URL,
                    payload="html",
                    timeout_seconds=self.transport.deadline(12),
                )
            ) as page_task,
            _background(
                coffee.request(
                    self.transport,
                    client,
                    COFFEE_TRACE_URL,
                    payload="text",
                    timeout_seconds=self.transport.deadline(8),
                )
            ) as trace_task,
            _background(
                who_source.request(
                    self.transport,
                    client,
                    WHO_ME_URL,
                    timeout_seconds=self.transport.deadline(WHO_TIMEOUT_SECONDS),
                )
            ) as who_task,
            _background(
                ipx_source.request(
                    self.transport,
                    client,
                    IPX_SELF_URL,
                    timeout_seconds=self.transport.deadline(IPX_TIMEOUT_SECONDS),
                )
            ) as ipx_task,
            _background(gpt_checks.requests(self.transport, client)) as gpt_task,
        ):
            exit_ip, stage = await self._ip_stage(client, trace_task, who_task, ipx_task)
            trace = trace_task.result()
            gpt_requests: dict[str, dict[str, Any]] = {}
            if stage is not None:
                if on_exit_ip is not None:
                    on_exit_ip()
                try:
                    lookup = await stage.tasks["lookup"]
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
                    global_ping = await stage.tasks["global_ping"]
                    port_scan = await stage.tasks["port_scan"]
                    ping_check = await stage.tasks["ping_check"]
                    gpt_requests = await gpt_task
                    ipure = await stage.tasks["ipure"]
                    ipqs = await stage.tasks["ipqs"]
                    ipx = await stage.tasks["ipx"]
                finally:
                    await stage.cancel()
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
                ipx = _missing_result("", reason, skipped=True)
            page = await page_task
            who = await who_task

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
                "ipx": ipx,
                "who": who,
                **(
                    {"related": related}
                    if lookup_data.get("related_pending")
                    or lookup_data.get("related_domains_pending")
                    else {}
                ),
            },
        )
        final_exit_ip = exit_ip if status != "failed" else None
        # 没有出口 IP 时 who 的失败只是同一个连接故障的回声，不再并入失败原因。
        error = _combined_error(
            page, trace, lookup, global_ping, port_scan, ping_check, related, ipure, ipqs,
            *((who, ipx) if exit_ip else ()),
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
            "ipx": ipx,
            "who": who,
        }
        completeness = _completeness(request_results, exit_ip, lookup_data)
        gpt_check = _gpt_check_summary(gpt_requests, lookup_data)
        coffee_summary = _profile_summary(lookup_data)
        ipure_scores = _ipure_scores(ipure)
        identity = _network_identity(
            exit_ip if status != "failed" else "",
            who.get("data"),
            _ipure_field(ipure, "network"),
            ipqs.get("data"),
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
            **ipqs_source.ipqs_fields(ipqs),
            **ipx_source.ipx_fields(ipx),
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
                "identity_origin": WHO_ORIGIN,
                "trust_env": False,
                "ipure_budget_remaining": ipure.get("budget_remaining"),
                "ipure_via_direct_fallback": bool(ipure.get("direct_fallback")),
                "ipqs_via_direct_fallback": bool(ipqs.get("direct_fallback")),
                "ipx_via_direct_fallback": bool(ipx.get("direct_fallback")),
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

    async def _ip_stage(
        self,
        client: httpx.AsyncClient,
        trace_task: asyncio.Task[Any],
        who_task: asyncio.Task[Any],
        ipx_task: asyncio.Task[Any],
    ) -> tuple[str, _IpStage | None]:
        """等 trace 定出口 IP；期间 who / ipx /json 谁先回显 IP，就先拿它开跑按 IP 查询的请求。

        trace 仍是出口 IP 的唯一判据：它给的 IP 与抢跑用的不一致（多出口节点），抢跑的
        请求整组作废、按 trace 的 IP 重发；trace 没给出 IP，抢跑的请求同样作废。
        """

        stage: _IpStage | None = None
        echo_tasks = (who_task, ipx_task)
        pending: set[asyncio.Task[Any]] = {trace_task, *echo_tasks}
        try:
            while not trace_task.done():
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in echo_tasks:
                    if stage is None and task in done and (ip := _echoed_ip(task.result())):
                        stage = self._start_ip_stage(client, ip, ipx_task)
            exit_ip = _trace_ip(trace_task.result().get("data"))
            if stage is not None and stage.ip != exit_ip:
                await stage.cancel()
                stage = None
            if exit_ip and stage is None:
                stage = self._start_ip_stage(client, exit_ip, ipx_task)
            return exit_ip, stage
        except BaseException:
            if stage is not None:
                await stage.cancel()
            raise

    def _start_ip_stage(
        self, client: httpx.AsyncClient, ip: str, ipx_task: asyncio.Task[Any]
    ) -> _IpStage:
        async def ipx_report() -> dict[str, Any]:
            # shield：这一组作废时只取消按 IP 的补查，不能连带取消第 0 秒发出的 ipx /json。
            proxied = await asyncio.shield(ipx_task)
            return await ipx_source.for_exit_ip(self.transport.proxy_url, proxied, ip)

        def request(url: str, cap_seconds: float) -> Coroutine[Any, Any, dict[str, Any]]:
            return coffee.request(
                self.transport,
                client,
                url,
                payload="json",
                timeout_seconds=self.transport.deadline(cap_seconds),
            )

        coroutines = {
            "lookup": request(f"{COFFEE_ORIGIN}/api/ip/lookup/{quote(ip, safe='')}", 45),
            "global_ping": request(_global_ping_url(ip), 8),
            "port_scan": request(f"{COFFEE_ORIGIN}/api/ip/portscan/{ip}?probe=0", 8),
            "ping_check": request(f"{COFFEE_ORIGIN}/api/ip/pingcheck/{ip}", 8),
            "ipure": ipure_source.request(
                self.transport,
                client,
                _ipure_url(ip),
                timeout_seconds=self.transport.deadline(IPURE_TIMEOUT_SECONDS),
            ),
            "ipqs": ipqs_source.request(
                self.transport,
                client,
                ipqs_source.ipqs_url(ip),
                timeout_seconds=self.transport.deadline(IPQS_TIMEOUT_SECONDS),
            ),
            "ipx": ipx_report(),
        }
        return _IpStage(ip, {name: asyncio.create_task(c) for name, c in coroutines.items()})


@dataclass(slots=True)
class _IpStage:
    """一组按出口 IP 的请求（lookup / ping / 端口 / IPure / IPQS / ipx 核对），可整组作废。"""

    ip: str
    tasks: dict[str, asyncio.Task[Any]]

    async def cancel(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def _echoed_ip(result: dict[str, Any]) -> str:
    """who / ipx /json 回显的连接 IP，规范成与 trace 相同的写法；没有就是空串。"""

    data = result.get("data") if result.get("ok") else None
    try:
        return str(ipaddress.ip_address(data.get("ip"))) if isinstance(data, dict) else ""
    except (TypeError, ValueError):
        return ""


@asynccontextmanager
async def _background(coro: Coroutine[Any, Any, Any]) -> AsyncIterator[asyncio.Task[Any]]:
    """Run a request alongside the block; never leave it pending when the block exits early."""

    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _network_identity(
    exit_ip: str,
    who_data: Any,
    ipure_network: Any,
    ipqs_data: Any,
    coffee_summary: dict[str, Any],
) -> dict[str, Any]:
    """出口 IP / 国家和地区 / 服务商 / 服务器名：1ms who 优先，不再回落 Coffee。

    地区与服务商：who（经节点代理问 Cloudflare 看到的连接本身）→ 1ms IPQS 多源共识 → IPure。
    who 回显的 ip 必须与 trace 出口一致才采信：多出口节点上两次连接可能走不同地址，
    那时 who 说的是另一个 IP，整组让给按 IP 查询的来源。
    服务器名（rdns）：who 的 PTR → 1ms 共识的 host。
    接入与原生性：1ms IPQS 多源共识 → IPure → Coffee（这两组 who 不提供）。

    出口 IP 仍以 Coffee trace 为准（who / ipx 先回显时按 IP 查询可以抢跑，不一致即作废重发），
    who / IPure 回显一致时记为由它确认。每一组各自记来源（network_source），
    页面与审计都能看出这一格是谁说的。
    """

    who = who_data if isinstance(who_data, dict) else {}
    if not (exit_ip and _is_same_ip(who.get("ip"), exit_ip)):
        who = {}
    network = ipure_network if isinstance(ipure_network, dict) else {}
    ipqs = ipqs_data if isinstance(ipqs_data, dict) else {}
    identity: dict[str, Any] = {}
    source: dict[str, str | None] = {}

    if not exit_ip:
        source["exit_ip"] = None
    elif who:
        source["exit_ip"] = "who"
    elif network.get("ip") and _is_same_ip(network.get("ip"), exit_ip):
        source["exit_ip"] = "ipure"
    else:
        source["exit_ip"] = "coffee"

    # 地区组：国家代码在哪个来源，整组就用哪个来源。国名只有 IPure 给，代码一致时借用；
    # 否则留空，页面按国家代码本地化显示。
    if who.get("country_code"):
        source["geo"] = "who"
        geo = {
            "country_code": who["country_code"],
            "region": who.get("region"),
            "city": who.get("city"),
        }
    elif ipqs.get("country"):
        source["geo"] = "ipqs"
        geo = {"country_code": ipqs["country"].upper(), "region": None, "city": ipqs.get("city")}
    elif network.get("country") or network.get("country_code"):
        source["geo"] = "ipure"
        geo = {
            "country_code": network.get("country_code"),
            "region": network.get("region"),
            "city": network.get("city"),
        }
    else:
        source["geo"] = None
        geo = {"country_code": None, "region": None, "city": None}
    ipure_code = _clean_text(network.get("country_code")).upper()
    same_country = source["geo"] == "ipure" or ipure_code == geo["country_code"]
    geo["country"] = network.get("country") if network.get("country") and same_country else None
    identity.update(geo)
    parts = (geo["country"] or geo["country_code"], geo["region"], geo["city"])
    identity["location"] = " ".join(part for part in dict.fromkeys(parts) if part)

    # 服务商组：ISP 显示运营这段地址的组织，AS 组织显示 AS 名；ASN 相同时借用 IPure 清洗过的 AS 名。
    ipure_isp = network.get("org") or network.get("as_name") or network.get("registry_org")
    ipure_as_name = network.get("as_name") or network.get("registry_org") or ipure_isp
    if who.get("asn") or who.get("as_org"):
        source["isp"] = "who"
        identity["asn"] = who.get("asn")
        identity["isp"] = who.get("as_org") or ""
        identity["as_org"] = (
            ipure_as_name
            if ipure_as_name and who.get("asn") and network.get("asn") == who.get("asn")
            else who.get("as_org") or ""
        )
    elif ipqs.get("asn") or ipqs.get("org"):
        source["isp"] = "ipqs"
        identity["asn"] = ipqs.get("asn")
        identity["isp"] = ipqs.get("org") or ""
        identity["as_org"] = ipqs.get("org") or ""
    elif ipure_isp or network.get("asn"):
        source["isp"] = "ipure"
        identity["asn"] = network.get("asn")
        identity["isp"] = ipure_isp or ""
        identity["as_org"] = ipure_as_name or ""
    else:
        source["isp"] = None
        identity.update(asn=None, isp="", as_org="")

    identity["rdns"] = who.get("ptr") or ipqs.get("host") or "-"

    # 接入：六档（住宅 / 移动 / 商业 / 机房 / 教育 / 政府），1ms → IPure → Coffee。
    if ipqs.get("type"):
        source["kind"] = "ipqs"
        identity["usage_type"] = ipqs["type"]
        identity["is_residential"] = ipqs["type"] in ("residential", "mobile")
        identity["is_datacenter"] = ipqs["type"] == "hosting"
    elif network.get("usage_type"):
        source["kind"] = "ipure"
        identity["usage_type"] = network["usage_type"]
        identity["is_residential"] = network["is_residential"]
        identity["is_datacenter"] = network["usage_type"] == "hosting"
    else:
        source["kind"] = (
            "coffee" if isinstance(coffee_summary.get("is_residential"), bool) else None
        )

    # 原生性：1ms → IPure → Coffee；广播时用登记国补出「广播 IP (XX)」。
    if isinstance(ipqs.get("native"), bool):
        source["native"] = "ipqs"
        identity["is_native"] = ipqs["native"]
        _describe_native(
            identity, ipqs["native"], ipqs.get("registered_country"), ipqs.get("country")
        )
    elif isinstance(network.get("is_native"), bool):
        source["native"] = "ipure"
        identity["is_native"] = network["is_native"]
        _describe_native(
            identity, network["is_native"], network.get("registered_country"), geo["country_code"]
        )
    else:
        source["native"] = "coffee" if isinstance(coffee_summary.get("is_native"), bool) else None

    identity["network_source"] = source
    return identity


def _describe_native(
    identity: dict[str, Any], native: bool, registered: Any, located: Any
) -> None:
    """原生 / 广播的展示文案；广播时写明登记国与归属地，读表的人能看出差在哪。"""

    if native:
        identity["native_status"] = "原生 IP"
        identity["native_detail"] = ""
        return
    identity["native_status"] = f"广播 IP ({registered})" if registered else "广播 IP"
    identity["native_detail"] = (
        f"IP注册在 {registered} 和IP归属地 {located or ''} 不一致".strip() if registered else ""
    )


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
        "ipx_recorded": _request_recorded(requests["ipx"]),
        "who_recorded": _request_recorded(requests["who"]),
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


_ENRICHMENT_HOSTS = frozenset({IPURE_HOST, IPQS_HOST, IPX_HOST, WHO_HOST})


def _combined_error(*results: dict[str, Any]) -> str | None:
    errors = [
        result.get("error")
        for result in results
        if result.get("error")
        and (not result.get("skipped") or result.get("target_host") in _ENRICHMENT_HOSTS)
    ]
    return "；".join(dict.fromkeys(errors)) or None


def _now() -> str:
    return datetime.now(UTC).isoformat()
