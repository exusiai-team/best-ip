from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, unquote, urlsplit

from backend.app.sources import collector as collector_module
from backend.app.sources.collector import CoffeeCollector

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
    """Fake every source; trace answers after `trace_delay`, who echoes `who_ip` at once."""

    def __init__(self, monkeypatch, *, trace_ip, who_ip, trace_delay=0.05):
        self.calls: list[tuple[str, str, bool]] = []
        self.cancelled: list[str] = []
        self.trace_done = False
        upstream = self

        async def coffee_request(transport, client, url, *, payload, timeout_seconds):
            if url.endswith("/cdn-cgi/trace"):
                await asyncio.sleep(trace_delay)
                upstream.trace_done = True
                return _ok(url, f"ip={trace_ip}\n" if trace_ip else "h=x\n")
            if url.endswith("/ip/"):
                return _ok(url, "<html></html>")
            return await upstream.by_ip("coffee", url, {"ip": _ip_in(url)})

        async def who_request(transport, client, url, *, timeout_seconds):
            return _ok(url, {"ip": who_ip} if who_ip else None)

        async def ipx_request(transport, client, url, *, timeout_seconds):
            await asyncio.sleep(0.2)
            return _ok(url, None)

        async def ipx_for_exit_ip(proxy_url, proxied, exit_ip):
            return _ok("ipx", {"ip": exit_ip})

        async def ipure_request(transport, client, url, *, timeout_seconds):
            return await upstream.by_ip("ipure", url, None)

        async def ipqs_request(transport, client, url, *, timeout_seconds):
            return await upstream.by_ip("ipqs", url, {"ip": _ip_in(url)})

        async def gpt_requests(transport, client):
            return {}

        async def related_result(transport, client, exit_ip, lookup_data):
            return _ok("related", None)

        module = collector_module
        monkeypatch.setattr(module.coffee, "request", coffee_request)
        monkeypatch.setattr(module.coffee, "related_result", related_result)
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


async def _collect():
    collector = CoffeeCollector("http://127.0.0.1:7890", timeout_ms=5000)
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
    assert record["requests"]["ipx"]["data"] == {"ip": TRACE_IP}
    assert {ip for _, ip, after_trace in upstream.calls if after_trace} == {TRACE_IP}
    assert "ipqs:" + OTHER_IP in upstream.cancelled


async def test_no_trace_ip_discards_speculative_lookups(monkeypatch):
    upstream = _Upstream(monkeypatch, trace_ip="", who_ip=OTHER_IP)
    record = await _collect()
    assert record["status"] == "failed"
    assert record["exit_ip"] is None
    assert record["requests"]["ipqs"]["skipped"] is True
    assert "ipqs:" + OTHER_IP in upstream.cancelled
