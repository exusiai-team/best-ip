from __future__ import annotations

import asyncio
import inspect
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
)
from .gpt_checks import _gpt_check_summary
from .http import (
    COFFEE_ORIGIN,
    COFFEE_TRACE_URL,
    IPQS_TIMEOUT_SECONDS,
    IPURE_ORIGIN,
    IPURE_TIMEOUT_SECONDS,
    IPX_ORIGIN,
    IPX_SELF_URL,
    IPX_TIMEOUT_SECONDS,
    WHO_ME_URL,
    WHO_TIMEOUT_SECONDS,
    ProxyTransport,
)
from .ip_cache import IpCache
from .ipure import IPURE_NOT_INDEXED, _ipure_field, _ipure_scores, _ipure_url
from .registry import ENRICHMENT_HOSTS, SCORE_SOURCES, SOURCE_BY_NAME, SOURCES
from .values import _clean_text


class CoffeeCollector:
    """Coordinate dependent source requests and assemble record schema v1."""

    def __init__(
        self, proxy_url: str, *, timeout_ms: int, ip_cache: IpCache | None = None
    ) -> None:
        self.transport = ProxyTransport(proxy_url, timeout_ms=timeout_ms)
        # 同一任务内各节点共用：同一出口 IP 的按 IP 查询只问一次（见 ip_cache.py）。
        self.ip_cache = ip_cache

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
        """on_exit_ip：出口 IP 定下、开始等 IP 资料的那一刻回调一次（只给实时进度用）。"""
        started_at = _now()
        started = perf_counter()
        # 不需要出口 IP 的请求（trace、who、ipx /json、GPT 探测）在第 0 秒一起发出。
        async with (
            self.transport.client() as client,
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
                            "error": "Coffee lookup 未返回与出口 IP 一致的数据",
                        }
                        lookup_data = {}
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
                reason = "未取得出口 IP"
                lookup = _missing_result("", reason, skipped=True)
                lookup_data = {}
                global_ping = _missing_result("", reason, skipped=True)
                port_scan = _missing_result("", reason, skipped=True)
                ping_check = _missing_result("", reason, skipped=True)
                ipure = _missing_result("", reason, skipped=True)
                ipqs = _missing_result("", reason, skipped=True)
                ipx = _missing_result("", reason, skipped=True)
            who = await who_task

        request_results = {
            "trace": trace,
            "lookup": lookup,
            "global_ping": global_ping,
            "port_scan": port_scan,
            "ping_check": ping_check,
            "ipure": ipure,
            "ipqs": ipqs,
            "ipx": ipx,
            "who": who,
        }
        completeness = _completeness(request_results, exit_ip, lookup_data)
        status = _node_status(exit_ip, completeness)
        final_exit_ip = exit_ip or None
        # 没有出口 IP 时 who / ipx 的失败只是同一个连接故障的回声，不再并入失败原因。
        error = _combined_error(
            trace, lookup, global_ping, port_scan, ping_check, ipure, ipqs,
            *((who, ipx) if exit_ip else ()),
        )
        gpt_check = _gpt_check_summary(gpt_requests, lookup_data)
        coffee_summary = _profile_summary(lookup_data)
        ipure_scores = _ipure_scores(ipure)
        identity = _network_identity(
            exit_ip,
            stage.source if stage is not None else "",
            ipx.get("data"),
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
            "related_domains": _related_domains(lookup_data),
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
                "identity_origin": IPX_ORIGIN,
                # 出口 IP 是谁先定下的：Coffee trace，trace 没给出时退回 ipx / who 的回显。
                "exit_ip_source": (stage.source or None) if stage is not None else None,
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
                "trace": trace.get("data"),
                "lookup": lookup.get("data"),
                "global_ping": global_ping.get("data"),
                "port_scan": port_scan.get("data"),
                "ping_check": ping_check.get("data"),
                "gpt_check": gpt_check,
            },
        }

    async def _ip_stage(
        self,
        client: httpx.AsyncClient,
        trace_task: asyncio.Task[Any],
        who_task: asyncio.Task[Any],
        ipx_task: asyncio.Task[Any],
    ) -> tuple[str, _IpStage | None]:
        """定出口 IP；期间 who / ipx /json 谁先回显 IP，就先拿它开跑按 IP 查询的请求。

        trace 给出 IP 时以它为准：与抢跑用的不一致（多出口节点）就整组作废、按 trace 重发。
        trace 没给出 IP（Coffee 不通或限流）时，退回节点代理上的回显——抢跑那组的 IP，
        或等 ipx / who 答完取第一个回显（ipx 优先）。它们问的都是「这条连接的出口」，
        所以一家上游出故障不会让整个节点判失败。
        """

        stage: _IpStage | None = None
        echo_tasks = {"ipx": ipx_task, "who": who_task}
        pending: set[asyncio.Task[Any]] = {trace_task, *echo_tasks.values()}
        try:
            while not trace_task.done():
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for name, task in echo_tasks.items():
                    if stage is None and task in done and (ip := _echoed_ip(task.result())):
                        stage = self._start_ip_stage(client, ip, name, ipx_task)
            exit_ip = _trace_ip(trace_task.result().get("data"))
            if exit_ip:
                if stage is not None and stage.ip != exit_ip:
                    await stage.cancel()
                    stage = None
                if stage is None:
                    stage = self._start_ip_stage(client, exit_ip, "trace", ipx_task)
                else:
                    stage.source = "trace"
                return exit_ip, stage
            if stage is None:
                await asyncio.wait(echo_tasks.values())
                for name, task in echo_tasks.items():
                    if ip := _echoed_ip(task.result()):
                        stage = self._start_ip_stage(client, ip, name, ipx_task)
                        break
            return (stage.ip if stage is not None else ""), stage
        except BaseException:
            if stage is not None:
                await stage.cancel()
            raise

    def _start_ip_stage(
        self, client: httpx.AsyncClient, ip: str, source: str, ipx_task: asyncio.Task[Any]
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
            # 实测 p90 0.9s、最慢 2.1s；旧的 45 秒上限只会让卡住的 lookup 占着整个节点。
            "lookup": request(f"{COFFEE_ORIGIN}/api/ip/lookup/{quote(ip, safe='')}", 10),
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
        def shared(name: str, coroutine: Coroutine[Any, Any, dict[str, Any]]) -> Any:
            if self.ip_cache is None or not SOURCE_BY_NAME[name].shareable:
                return coroutine
            cache = self.ip_cache

            async def fetch() -> dict[str, Any]:
                try:
                    return await cache.fetch(name, ip, lambda: coroutine)
                finally:
                    # 复用了别人的答案时这个协程从没跑过：关掉它，免得留下「从未 await」的告警。
                    if inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
                        coroutine.close()

            return fetch()

        return _IpStage(
            ip,
            source,
            {name: asyncio.create_task(shared(name, c)) for name, c in coroutines.items()},
        )


@dataclass(slots=True)
class _IpStage:
    """一组按出口 IP 的请求（lookup / ping / 端口 / IPure / IPQS / ipx 核对），可整组作废。

    source：这个 IP 由谁定下（trace / ipx / who）。
    """

    ip: str
    source: str
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
    exit_ip_source: str,
    ipx_data: Any,
    who_data: Any,
    ipure_network: Any,
    ipqs_data: Any,
    coffee_summary: dict[str, Any],
) -> dict[str, Any]:
    """出口 IP / 国家和地区 / 服务商 / 服务器名 / 接入 / 原生性：ipx.al 优先。

    每一组的顺序：
    - 地区：ipx → who（经节点代理问 Cloudflare）→ 1ms IPQS 共识 → IPure；
    - 服务商：ipx（ASN + RIR 组织名）→ who → 1ms IPQS → IPure；
    - 服务器名（rdns）：ipx hostname → who PTR → 1ms host；
    - 接入与原生性：ipx（多源投票）→ 1ms IPQS → IPure → Coffee。
    ipx 与 who 的回显 IP 必须与出口 IP 一致才采信：多出口节点上两次连接可能走不同地址，
    那时它们说的是另一个 IP（ipx 已在 `for_exit_ip` 里改为按 IP 查询，who 则整组让出）。

    exit_ip 的来源记的是「谁确认了它」：ipx / who 回显一致时记它，否则记定下它的那一家
    （trace 记为 coffee）。每一组各自记来源（network_source），页面与审计都能看出这一格是谁说的。
    """

    ipx = ipx_data if isinstance(ipx_data, dict) else {}
    if not (exit_ip and _is_same_ip(ipx.get("ip"), exit_ip)):
        ipx = {}
    who = who_data if isinstance(who_data, dict) else {}
    if not (exit_ip and _is_same_ip(who.get("ip"), exit_ip)):
        who = {}
    network = ipure_network if isinstance(ipure_network, dict) else {}
    ipqs = ipqs_data if isinstance(ipqs_data, dict) else {}
    identity: dict[str, Any] = {}
    source: dict[str, str | None] = {}

    if not exit_ip:
        source["exit_ip"] = None
    elif ipx:
        source["exit_ip"] = "ipx"
    elif who:
        source["exit_ip"] = "who"
    elif network.get("ip") and _is_same_ip(network.get("ip"), exit_ip):
        source["exit_ip"] = "ipure"
    else:
        source["exit_ip"] = "coffee" if exit_ip_source == "trace" else exit_ip_source or None

    # 地区组：国家代码在哪个来源，整组就用哪个来源。国名只有 IPure 给，代码一致时借用；
    # 否则留空，页面按国家代码本地化显示。ipx 不给省/州，who 同国时借它的 region。
    if ipx.get("country_code"):
        source["geo"] = "ipx"
        geo = {
            "country_code": ipx["country_code"],
            "region": who.get("region") if who.get("country_code") == ipx["country_code"] else None,
            "city": ipx.get("city"),
        }
    elif who.get("country_code"):
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
    if ipx.get("asn") or ipx.get("isp"):
        source["isp"] = "ipx"
        identity["asn"] = ipx.get("asn")
        identity["isp"] = ipx.get("isp") or ""
        identity["as_org"] = ipx.get("as_org") or ipx.get("isp") or ""
    elif who.get("asn") or who.get("as_org"):
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

    identity["rdns"] = ipx.get("hostname") or who.get("ptr") or ipqs.get("host") or "-"

    # 接入：六档（住宅 / 移动 / 商业 / 机房 / 教育 / 政府），ipx → 1ms → IPure → Coffee。
    for name, usage, residential in (
        ("ipx", ipx.get("usage_type"), None),
        ("ipqs", ipqs.get("type"), None),
        ("ipure", network.get("usage_type"), network.get("is_residential")),
    ):
        if usage:
            source["kind"] = name
            identity["usage_type"] = usage
            identity["is_residential"] = (
                residential if residential is not None else usage in ("residential", "mobile")
            )
            identity["is_datacenter"] = usage == "hosting"
            break
    else:
        source["kind"] = (
            "coffee" if isinstance(coffee_summary.get("is_residential"), bool) else None
        )

    # 原生性：ipx → 1ms → IPure → Coffee；广播时用登记国补出「广播 IP (XX)」。
    for name, native, registered, located in (
        ("ipx", ipx.get("native"), ipx.get("registered_country"), geo["country_code"]),
        ("ipqs", ipqs.get("native"), ipqs.get("registered_country"), ipqs.get("country")),
        ("ipure", network.get("is_native"), network.get("registered_country"),
         geo["country_code"]),
    ):
        if isinstance(native, bool):
            source["native"] = name
            identity["is_native"] = native
            _describe_native(identity, native, registered, located)
            break
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


def _node_status(exit_ip: str, completeness: dict[str, Any]) -> str:
    """没有出口 IP 才算失败；有出口 IP 而缺了某个分数来源，算部分。"""

    if not exit_ip:
        return "failed"
    return "partial" if completeness["partial_reasons"] else "success"


def _completeness(
    requests: dict[str, dict[str, Any]],
    exit_ip: str,
    lookup_data: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "trace_received": bool(requests["trace"].get("ok")),
        "exit_ip_valid": bool(exit_ip),
        "lookup_received": bool(requests["lookup"].get("ok")),
        "lookup_matches_exit_ip": bool(exit_ip and _is_same_ip(lookup_data.get("ip"), exit_ip)),
        **{
            source.check: _request_recorded(requests[source.name])
            for source in SOURCES
            if source.check.endswith("_recorded")
        },
    }
    missing = [] if checks["exit_ip_valid"] else ["exit_ip_valid"]
    unrecorded = [
        name for name, value in checks.items() if name.endswith("_recorded") and not value
    ]
    # 页面上各占一列的分数来源缺了才算「部分」；who、ping、端口、连通检测只作证据。
    # IPure 明确答复「没收录这个 IP」是上游对这个地址的结论，不是采集缺口。
    partial_reasons = [
        source.check
        for source in SCORE_SOURCES
        if exit_ip
        and not checks[source.check]
        and requests[source.name].get("error_type") != IPURE_NOT_INDEXED
    ]
    return {
        "complete": not missing and not unrecorded and checks["lookup_received"],
        "checks": checks,
        "missing_required": missing,
        "unrecorded_requests": unrecorded,
        "partial_reasons": partial_reasons,
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
        and (not result.get("skipped") or result.get("target_host") in ENRICHMENT_HOSTS)
    ]
    return "；".join(dict.fromkeys(errors)) or None


def _now() -> str:
    return datetime.now(UTC).isoformat()
