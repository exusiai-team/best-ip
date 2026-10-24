from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.mihomo import MihomoError
from backend.app.results.validation import validate_node
from backend.app.scan.node_runner import (
    NodeRunner,
    _dns_bootstrap_candidates,
    _dns_bootstrap_for_attempt,
)

from .conftest import JOB_ID, success_record


class FakeMihomo:
    instances: list[FakeMihomo] = []

    def __init__(self, path, work_dir: Path, proxies, *, selector_names, outbound_interface,
                 dns_bootstrap_proxy):
        self.work_dir = work_dir
        self.proxies = proxies
        self.outbound_interface = outbound_interface
        self.dns_bootstrap_proxy = dns_bootstrap_proxy
        self.proxy_url = "http://127.0.0.1:17890"
        self.instance_id = f"fake-{len(FakeMihomo.instances)}"
        self.stopped = False
        FakeMihomo.instances.append(self)

    async def start(self):
        self.work_dir.mkdir(parents=True)

    def log_offset(self):
        return 0

    def read_log_since(self, offset):
        return ""

    async def select(self, name):
        return name

    async def stop(self):
        self.stopped = True


def collector_factory(plan):
    """plan[n] 是第 n 次 collect 的行为：'ok'、'fail' 或 ('slow', 秒数)。"""

    calls = []

    class FakeCollector:
        def __init__(self, proxy_url, *, timeout_ms):
            pass

        async def collect(self, *, job_id, node_index, node_name, **_):
            attempt = len(calls)
            calls.append(attempt)
            action = plan[attempt]
            if isinstance(action, tuple):
                await asyncio.sleep(action[1])
                action = "ok"
            if action == "fail":
                raise MihomoError("Mihomo 节点连接失败")
            return success_record(node_index, job_id=job_id, name=node_name)

    FakeCollector.calls = calls
    return FakeCollector


def _runner(collector, **overrides):
    FakeMihomo.instances = []
    settings = Settings(
        mihomo_path=Path("/nonexistent/mihomo"),
        node_retry_backoff_ms=0,
        **overrides,
    )
    return NodeRunner(settings, mihomo_factory=FakeMihomo, collector_factory=collector)


async def _run(runner, tmp_path):
    proxy = {"name": "node-0", "type": "ss", "server": "203.0.113.1"}
    return await runner.run(
        job_id=JOB_ID,
        work_dir=tmp_path,
        proxies=[proxy],
        proxy=proxy,
        index=0,
        dns_bootstrap_candidates=[],
        outbound_interface=None,
    )


async def test_first_attempt_success_uses_one_mihomo(tmp_path):
    collector = collector_factory(["ok"])
    outcome = await _run(_runner(collector), tmp_path)
    assert outcome.record["status"] == "success"
    assert outcome.record["attempt_count"] == 1
    assert outcome.error is None
    assert len(FakeMihomo.instances) == 1
    assert all(instance.stopped for instance in FakeMihomo.instances)
    assert not any(tmp_path.iterdir()), "每次尝试的工作目录都要清理掉"
    validate_node(JOB_ID, 0, outcome.record)


async def test_retry_runs_remaining_attempts_in_parallel(tmp_path):
    # 第 1 次失败；第 2 次慢、第 3 次快：第 3 次胜出，第 2 次被取消且不计入尝试数。
    collector = collector_factory(["fail", ("slow", 5), "ok"])
    outcome = await asyncio.wait_for(_run(_runner(collector), tmp_path), timeout=2)
    record = outcome.record
    assert record["status"] == "success"
    assert record["attempt_count"] == 2
    assert record["retry_count"] == 1
    assert len(record["attempt_errors"]) == 1
    assert len(FakeMihomo.instances) == 3
    assert all(instance.stopped for instance in FakeMihomo.instances)
    validate_node(JOB_ID, 0, record)


async def test_all_attempts_fail_reports_failed_record(tmp_path):
    collector = collector_factory(["fail", "fail", "fail"])
    outcome = await _run(_runner(collector), tmp_path)
    record = outcome.record
    assert record["status"] == "failed"
    assert record["attempt_count"] == 3
    assert len(record["attempt_errors"]) == 3
    assert outcome.error is not None and outcome.error.code == "node_transport"
    assert all(instance.stopped for instance in FakeMihomo.instances)
    validate_node(JOB_ID, 0, record)


async def test_single_attempt_setting_disables_retry(tmp_path):
    collector = collector_factory(["fail"])
    outcome = await _run(_runner(collector, max_node_attempts=1), tmp_path)
    assert outcome.record["attempt_count"] == 1
    assert len(collector.calls) == 1


def test_dns_bootstrap_candidates_only_global_ipv4():
    proxies = [
        {"name": "v4", "server": "8.8.8.8"},
        {"name": "private", "server": "10.0.0.1"},
        {"name": "v6", "server": "2001:4860::8888"},
        {"name": "domain", "server": "example.com"},
        {"name": "v4", "server": "8.8.8.8"},
    ]
    assert _dns_bootstrap_candidates(proxies) == ["v4"]


@pytest.mark.parametrize(
    ("server", "expected"),
    [("example.com", ["a", "b", "a"]), ("8.8.8.8", [None, None, None])],
)
def test_dns_bootstrap_rotates_per_attempt_only_for_domain_servers(server, expected):
    proxy = {"name": "n", "server": server}
    picks = [
        _dns_bootstrap_for_attempt(proxy, ["a", "b"], node_index=0, attempt=attempt)
        for attempt in (1, 2, 3)
    ]
    assert picks == expected
