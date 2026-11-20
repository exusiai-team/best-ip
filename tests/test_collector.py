from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, unquote, urlsplit

from backend.app.sources import collector as collector_module
from backend.app.sources.collector import CoffeeCollector
from backend.app.sources.ip_cache import IpCache

TRACE_IP = "203.0.113.7"
OTHER_IP = "198.51.100.9"


def _ok(url, data):
    return {"ok": True, "attempted": True, "status_code": 200, "url": url, "data": data}


def _ip_in(url):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for key in ("ip", "host"):
        if key in query:
            return query[key][0]
    return unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])


class _Upstream:
    """Fake every source; trace answers after `trace_delay`, who echoes `who_ip` at once,
    ipx `/json` echoes `ipx_ip` after 0.2s; IPure answers `ipure_result` when given."""

    def __init__(
        self, monkeypatch, *, trace_ip, who_ip, ipx_ip="", trace_delay=0.05, ipure_result=None
    ):
        self.calls: list[tuple[str, str, bool]] = []
        self.cancelled: list[str] = []
        self.trace_done = False
        upstream = self

        async def coffee_request(transport, client, url, *, payload, timeout_seconds):
            if url.endswith("/cdn-cgi/trace"):
                await asyncio.sleep(trace_delay)
                upstream.trace_done = True
                return _ok(url, f"ip={trace_ip}\n" if trace_ip else "h=x\n")
            return await upstream.by_ip("coffee", url, {"ip": _ip_in(url)})

        async def who_request(transport, client, url, *, timeout_seconds):
            return _ok(url, {"ip": who_ip} if who_ip else None)

        async def ipx_request(transport, client, url, *, timeout_seconds):
            await asyncio.sleep(0.2)
            return _ok(url, {"ip": ipx_ip} if ipx_ip else None)

        async def ipx_for_exit_ip(proxy_url, proxied, exit_ip):
            return _ok("ipx", {"ip": exit_ip, "score": 80, "country_code": "JP", "asn": 2516})

        async def ipure_request(transport, client, url, *, timeout_seconds):
            if ipure_result is not None:
                return ipure_result
            return await upstream.by_ip("ipure", url, None)

        async def ipqs_request(transport, client, url, *, timeout_seconds):
            return await upstream.by_ip("ipqs", url, {"ip": _ip_in(url)})

        async def gpt_requests(transport, client):
            return {}

        module = collector_module
        monkeypatch.setattr(module.coffee, "request", coffee_request)
        monkeypatch.setattr(module.who_source, "request", who_request)
        monkeypatch.setattr(module.ipx_source, "request", ipx_request)
        monkeypatch.setattr(module.ipx_source, "for_exit_ip", ipx_for_exit_ip)
        monkeypatch.setattr(module.ipure_source, "request", ipure_request)
        monkeypatch.setattr(module.ipqs_source, "request", ipqs_request)
        monkeypatch.setattr(module.gpt_checks, "requests", gpt_requests)

    async def by_ip(self, source, url, data):
        self.calls.append((source, _ip_in(url), self.trace_done))
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            self.cancelled.append(f"{source}:{_ip_in(url)}")
            raise
        return _ok(url, data)


async def _collect(ip_cache=None, port=7890, family_probe=None):
    collector = CoffeeCollector(
        f"http://127.0.0.1:{port}", timeout_ms=5000, ip_cache=ip_cache, family_probe=family_probe
    )
    return await collector.collect(
        job_id="job",
        node_index=0,
        node_name="node",
        node_type="ss",
        selected_proxy="node",
        mihomo_instance="m",
    )


async def test_ip_lookups_start_on_who_echo_before_trace(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    record = await _collect()
    assert record["exit_ip"] == TRACE_IP
    assert record["status"] != "failed"
    # lookup / global ping / portscan / pingcheck / IPure / IPQS 各发一次，全在 trace 返回之前。
    assert len(upstream.calls) == 6
    assert all(ip == TRACE_IP and not after_trace for _, ip, after_trace in upstream.calls)
    assert not upstream.cancelled


async def test_mismatched_echo_is_discarded_and_rerun_for_trace_ip(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=OTHER_IP)
    record = await _collect()
    assert record["exit_ip"] == TRACE_IP
    assert record["requests"]["ipqs"]["data"] == {"ip": TRACE_IP}
    assert record["requests"]["lookup"]["data"] == {"ip": TRACE_IP}
    assert record["requests"]["ipx"]["data"]["ip"] == TRACE_IP
    assert {ip for _, ip, after_trace in upstream.calls if after_trace} == {TRACE_IP}
    assert "ipqs:" + OTHER_IP in upstream.cancelled


async def test_no_trace_ip_falls_back_to_the_echoed_ip(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip="", who_ip=OTHER_IP)
    record = await _collect()
    assert record["status"] != "failed"
    assert record["exit_ip"] == OTHER_IP
    assert record["proxy_evidence"]["exit_ip_source"] == "who"
    assert record["requests"]["ipqs"]["data"] == {"ip": OTHER_IP}
    assert not upstream.cancelled


async def test_no_trace_ip_prefers_the_ipx_echo_over_who(monkeypatch):
    _Upstream(monkeypatch, trace_ip="", who_ip="", ipx_ip=OTHER_IP)
    record = await _collect()
    assert record["exit_ip"] == OTHER_IP
    assert record["proxy_evidence"]["exit_ip_source"] == "ipx"
    # 身份由 ipx 报告给出，出口 IP 记为 ipx 确认。
    assert record["network_source"]["exit_ip"] == "ipx"
    assert record["network_source"]["geo"] == "ipx"
    assert record["country_code"] == "JP"
    assert record["asn"] == 2516


async def test_no_ip_anywhere_fails_and_skips_ip_lookups(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip="", who_ip="")
    record = await _collect()
    assert record["status"] == "failed"
    assert record["exit_ip"] is None
    assert record["requests"]["ipqs"]["skipped"] is True
    assert not upstream.calls


async def test_coffee_page_and_related_are_no_longer_requested(monkeypatch):
    _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    record = await _collect()
    assert "page" not in record["requests"]
    assert "related" not in record["requests"]
    assert record["status"] == "success"
    assert record["completeness"]["partial_reasons"] == []


async def test_ipure_not_indexed_does_not_make_the_node_partial(monkeypatch):
    not_indexed = {
        "ok": False, "attempted": True, "status_code": 401, "url": "ipure", "data": None,
        "error": "IPure 未收录该 IP", "error_type": "NotIndexed", "skipped": True,
    }
    _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP, ipure_result=not_indexed)
    record = await _collect()
    assert record["status"] == "success"
    assert record["completeness"]["checks"]["ipure_recorded"] is False
    assert record["score"] is None


async def test_missing_score_source_makes_the_node_partial(monkeypatch):
    failed = {
        "ok": False, "attempted": True, "status_code": 500, "url": "ipure", "data": None,
        "error": "HTTP 500", "error_type": "HTTPStatusError", "skipped": False,
    }
    _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP, ipure_result=failed)
    record = await _collect()
    assert record["status"] == "partial"
    assert record["completeness"]["partial_reasons"] == ["ipure_recorded"]


V6_IP = "2001:db8::7"
FAMILY_PROBE = {4: "192.0.2.10", 6: "2001:db8:ffff::10"}


def _family_answers(monkeypatch, answers):
    """双栈探测替身：按所连 who 地址的族回 answers[族]（None = 这一族连不上）。"""

    probed = []

    async def request_family(proxy_url, address, *, timeout_seconds):
        version = 6 if ":" in address else 4
        probed.append(version)
        ip = answers.get(version)
        if ip is None:
            return {"ok": False, "skipped": True, "data": None, "error": "连接失败"}
        return {**_ok("who", {"ip": ip}), "via_mihomo": True}

    monkeypatch.setattr(collector_module.who_source, "request_family", request_family)
    return probed


async def test_dual_stack_node_gets_an_alt_exit_for_the_other_family(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    probed = _family_answers(monkeypatch, {4: TRACE_IP, 6: V6_IP})
    record = await _collect(family_probe=FAMILY_PROBE)
    alt = record["alt_exit"]
    assert sorted(probed) == [4, 6]
    assert record["exit_ip"] == TRACE_IP
    assert alt["exit_ip"] == V6_IP and alt["ip_version"] == 6
    assert alt["status"] == "success"
    assert alt["network_source"]["exit_ip"] == "ipx"
    assert alt["requests"]["who"]["data"]["ip"] == V6_IP
    assert alt["requests"]["ipqs"]["data"] == {"ip": V6_IP}
    assert alt["requests"]["lookup"]["data"] == {"ip": V6_IP}
    # 主出口与另一族各一组按 IP 查询（lookup / ping / 端口 / 连通 / IPure / IPQS）。
    assert sorted({ip for _, ip, _ in upstream.calls}) == sorted({TRACE_IP, V6_IP})
    assert len(upstream.calls) == 12


async def test_alt_exit_is_skipped_when_the_node_ignores_the_family(monkeypatch):
    # 节点按 SNI 重新解析、改走 IPv4：回显与所连地址不同族，不另起一行。
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    _family_answers(monkeypatch, {4: TRACE_IP, 6: TRACE_IP})
    record = await _collect(family_probe=FAMILY_PROBE)
    assert "alt_exit" not in record
    assert len(upstream.calls) == 6


async def test_alt_exit_is_skipped_without_an_other_family_exit(monkeypatch):
    _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    _family_answers(monkeypatch, {4: TRACE_IP, 6: None})
    record = await _collect(family_probe=FAMILY_PROBE)
    assert "alt_exit" not in record
    assert record["status"] == "success"


async def test_ipv6_default_exit_gets_an_ipv4_alt_exit(monkeypatch):
    _Upstream(monkeypatch, trace_ip=V6_IP, who_ip=V6_IP)
    _family_answers(monkeypatch, {4: TRACE_IP, 6: V6_IP})
    record = await _collect(family_probe=FAMILY_PROBE)
    assert record["exit_ip"] == V6_IP
    assert record["alt_exit"]["exit_ip"] == TRACE_IP
    assert record["alt_exit"]["ip_version"] == 4


async def test_nodes_sharing_an_exit_ip_ask_ip_lookups_once(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    cache = IpCache()
    first, second = await asyncio.gather(_collect(cache, 7890), _collect(cache, 7891))
    # lookup / global ping / portscan / pingcheck / IPure / IPQS：两个节点合起来各一次。
    assert len(upstream.calls) == 6
    shared = [
        name for name in ("lookup", "ipqs", "ipure", "global_ping", "port_scan", "ping_check")
        if first["requests"][name].get("shared") or second["requests"][name].get("shared")
    ]
    assert len(shared) == 6
    assert first["status"] == second["status"] == "success"


async def test_light_profile_skips_evidence_only_lookups_and_dual_stack(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    probed = _family_answers(monkeypatch, {4: TRACE_IP, 6: V6_IP})
    collector = CoffeeCollector(
        "http://127.0.0.1:7890", timeout_ms=5000, family_probe=FAMILY_PROBE, light=True
    )
    record = await collector.collect(
        job_id="job", node_index=0, node_name="node", node_type="ss",
        selected_proxy="node", mihomo_instance="m",
    )
    # 只发 IPv6 探测、只记 IPv6 出口地址，不为它另起一行、另查按 IP 资料
    assert probed == [6] and "alt_exit" not in record
    assert record["ipv6_exit_ip"] == V6_IP
    assert record["status"] == "success" and record["exit_ip"] == TRACE_IP
    # 只剩 lookup / IPure / IPQS 三个按 IP 查询；全球 Ping、端口、连通检测记为跳过。
    assert sorted(source for source, _, _ in upstream.calls) == ["coffee", "ipqs", "ipure"]
    for name in ("global_ping", "port_scan", "ping_check"):
        assert record["requests"][name]["skipped"] is True
        assert record["requests"][name]["attempted"] is False
    assert record["error"] is None


async def test_light_profile_without_ipv6_exit_records_nothing(monkeypatch):
    _Upstream(monkeypatch, trace_ip=TRACE_IP, who_ip=TRACE_IP)
    probed = _family_answers(monkeypatch, {4: TRACE_IP, 6: None})
    collector = CoffeeCollector(
        "http://127.0.0.1:7890", timeout_ms=5000, family_probe=FAMILY_PROBE, light=True
    )
    record = await collector.collect(
        job_id="job", node_index=0, node_name="node", node_type="ss",
        selected_proxy="node", mihomo_instance="m",
    )
    assert probed == [6] and "ipv6_exit_ip" not in record and "alt_exit" not in record
