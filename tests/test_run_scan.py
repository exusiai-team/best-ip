from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.scan.jobs import ScanJobManager
from backend.app.scan.node_runner import NodeRunner

from .test_node_runner import FakeMihomo, collector_factory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_scan  # noqa: E402

SUBSCRIPTION = b"""
proxies:
  - {name: alpha, type: ss, server: 8.8.8.8, port: 8388, cipher: aes-128-gcm, password: pw}
  - {name: beta, type: ss, server: 8.8.4.4, port: 8388, cipher: aes-128-gcm, password: pw}
  - {name: gamma, type: ss, server: 1.1.1.1, port: 8388, cipher: aes-128-gcm, password: pw}
"""


async def _no_family_probe() -> dict[int, str]:
    return {}


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    for name in (
        "BEST_IP_SUBSCRIPTION_RELAY_URL",
        "BEST_IP_SUBSCRIPTION_RELAY_TOKEN",
        "GITHUB_ACTIONS",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    ):
        monkeypatch.delenv(name, raising=False)


async def test_cli_scan_publishes_verified_artifact(tmp_path, capsys):
    mihomo = tmp_path / "mihomo"
    mihomo.write_bytes(b"")
    settings = Settings(mihomo_path=mihomo, node_retry_backoff_ms=0, max_parallel_nodes=2)
    # 每个节点的第 1 次 collect：alpha 成功、beta 失败后重试成功、gamma 三次都失败。
    plans = {"alpha": ["ok"], "beta": ["fail", "ok", "ok"], "gamma": ["fail"] * 3}
    collectors = {name: collector_factory(plan) for name, plan in plans.items()}

    class RoutingCollector:
        def __init__(self, proxy_url, *, timeout_ms, ip_cache=None):
            self.proxy_url = proxy_url

        async def collect(self, *, node_name, **kwargs):
            inner = collectors[node_name](self.proxy_url, timeout_ms=0)
            return await inner.collect(node_name=node_name, **kwargs)

    def manager_factory(app_settings, store, workspace, *, download):
        runner = NodeRunner(
            app_settings, mihomo_factory=FakeMihomo, collector_factory=RoutingCollector
        )
        return ScanJobManager(
            app_settings,
            store,
            workspace,
            node_runner=runner,
            download=download,
            resolve_interface=lambda configured: None,
            resolve_family_probe=_no_family_probe,
        )

    async def download(url, **_):
        return SUBSCRIPTION

    subscription_file = tmp_path / "subscription-url"
    subscription_file.write_text("https://sub.example.com/link?token=abcdef123456")
    output = tmp_path / "artifact"
    args = run_scan.parse_args(
        [
            "--request-id", "req-cli-1",
            "--subscription-file", str(subscription_file),
            "--output-dir", str(output),
            "--run-id", "7",
            "--run-attempt", "1",
        ]
    )
    await run_scan.run(
        args, app_settings=settings, manager_factory=manager_factory, download=download
    )

    result_bytes = (output / "result.json").read_bytes()
    status = json.loads((output / "status.json").read_bytes())
    assert status["result_sha256"] == hashlib.sha256(result_bytes).hexdigest()
    assert status["counts"] == {"success": 2, "partial": 0, "failed": 1, "complete": 2}
    result = json.loads(result_bytes)
    by_name = {record["node"]: record for record in result["results"]}
    assert by_name["beta"]["attempt_count"] == 2
    assert by_name["gamma"]["attempt_count"] == 3
    assert b"abcdef123456" not in result_bytes

    timing = [line for line in capsys.readouterr().err.splitlines() if "scan_timing" in line]
    assert len(timing) == 1
    assert "nodes=3" in timing[0] and "failed=1" in timing[0] and "retried=2" in timing[0]


def test_timing_summary_without_nodes():
    assert run_scan._timing_summary(812, {}, None) == "scan_timing: subscription=0.8s"


def test_timing_summary_reports_sources_without_ips():
    def record(ip, ipx_ms, direct=False):
        return {
            "exit_ip": ip,
            "requests": {
                "ipx": {"attempted": True, "ok": True, "elapsed_ms": ipx_ms,
                        "direct_fallback": direct},
                "trace": {"attempted": True, "ok": ip is not None, "elapsed_ms": 1000},
            },
        }

    records = [record("203.0.113.1", 4000), record("203.0.113.1", 6000, direct=True),
               record(None, 0)]
    line = run_scan._timing_summary(0, {}, None, records)
    assert "trace=2/3:1.0s/1.0s/1.0s" in line
    assert "ipx=3/3:4.0s/6.0s/6.0s" in line
    assert "direct_fallback[ipx=1]" in line
    assert "exit_ips=2 unique_exit_ips=1" in line
    assert "203.0.113" not in line


def _classify_manager_factory(collected):
    class RecordingCollector:
        def __init__(self, proxy_url, *, timeout_ms, ip_cache=None, light=False):
            self.light = light

        async def collect(self, *, job_id, node_index, node_name, **_):
            collected.append((node_name, self.light))
            from .conftest import success_record

            return success_record(node_index, job_id=job_id, name=node_name)

    def manager_factory(app_settings, store, workspace, *, download):
        runner = NodeRunner(
            app_settings, mihomo_factory=FakeMihomo, collector_factory=RecordingCollector
        )
        return ScanJobManager(
            app_settings,
            store,
            workspace,
            node_runner=runner,
            download=download,
            resolve_interface=lambda configured: None,
            resolve_family_probe=_no_family_probe,
        )

    return manager_factory


async def test_cli_classify_inline_yaml_writes_result(tmp_path, capsys):
    mihomo = tmp_path / "mihomo"
    mihomo.write_bytes(b"")
    settings = Settings(mihomo_path=mihomo, node_retry_backoff_ms=0, max_parallel_nodes=2)
    yaml_file = tmp_path / "subscription.yaml"
    yaml_file.write_bytes(SUBSCRIPTION.replace(b"name: gamma", b"name: alpha"))
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps({"label": "myclash:abcd1234", "reuse_hours": 0, "previous": None})
    )
    collected: list[tuple[str, bool]] = []
    output = tmp_path / "artifact"
    classify_output = tmp_path / "classify.json"
    args = run_scan.parse_args(
        [
            "--request-id", "clf-20261120-abcdef012345",
            "--subscription-yaml-file", str(yaml_file),
            "--output-dir", str(output),
            "--run-id", "7",
            "--run-attempt", "1",
            "--profile", "classify",
            "--classify-output", str(classify_output),
            "--classify-context", str(context),
        ]
    )
    await run_scan.run(
        args, app_settings=settings, manager_factory=_classify_manager_factory(collected)
    )
    # 重名节点在扫描里加后缀区分，轻量档传到了采集器。
    assert sorted(collected) == [("alpha", True), ("alpha #2", True), ("beta", True)]
    result = json.loads(classify_output.read_bytes())
    assert result["scan_id"] == "clf-20261120-abcdef012345"
    assert result["label"] == "myclash:abcd1234" and result["profile"] == "classify"
    assert [node["name"] for node in result["nodes"]] == ["alpha", "beta", "alpha"]
    assert [node["status"] for node in result["nodes"]] == ["ok", "ok", "ok"]
    assert result["input_sha256"] == hashlib.sha256(yaml_file.read_bytes()).hexdigest()
    assert (output / "result.json").is_file()
    err = capsys.readouterr().err
    assert "subscription_source: inline" in err and "classify_plan: nodes=3 scan=3" in err


async def test_cli_classify_all_reused_skips_the_scan(tmp_path):
    from backend.app.classify import fingerprint

    settings = Settings(mihomo_path=tmp_path / "missing", node_retry_backoff_ms=0)
    proxies = [
        {"name": name, "type": "ss", "server": server, "port": 8388, "password": "pw"}
        for name, server in (("alpha", "8.8.8.8"), ("beta", "8.8.4.4"), ("gamma", "1.1.1.1"))
    ]
    now = run_scan._iso_now()
    previous = {"nodes": [
        {"fingerprint": fingerprint(proxy), "status": "ok", "checked_at": now,
         "exit_ip": "203.0.113.9"}
        for proxy in proxies
    ]}
    yaml_file = tmp_path / "subscription.yaml"
    yaml_file.write_bytes(SUBSCRIPTION)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"label": "l", "reuse_hours": 20, "previous": previous}))
    output = tmp_path / "artifact"
    classify_output = tmp_path / "classify.json"
    args = run_scan.parse_args(
        [
            "--request-id", "clf-reuse",
            "--subscription-yaml-file", str(yaml_file),
            "--output-dir", str(output),
            "--run-id", "7",
            "--run-attempt", "1",
            "--profile", "classify",
            "--classify-output", str(classify_output),
            "--classify-context", str(context),
        ]
    )

    def no_manager(*_args, **_kwargs):
        raise AssertionError("全部复用时不该起扫描")

    await run_scan.run(args, app_settings=settings, manager_factory=no_manager)
    result = json.loads(classify_output.read_bytes())
    assert result["counts"] == {"ok": 3, "failed": 0, "skipped": 0, "reused": 3}
    assert not output.exists()


def test_failure_file_records_the_exit_code(tmp_path):
    failure = tmp_path / "failure"
    code = run_scan.main(
        [
            "--request-id", "bad id",
            "--subscription-file", str(tmp_path / "missing"),
            "--output-dir", str(tmp_path / "out"),
            "--failure-file", str(failure),
        ]
    )
    assert code == 2 and failure.read_text() == "input_invalid"


async def test_cli_classify_rescanned_nodes_all_failing_still_publishes_reused(tmp_path):
    from backend.app.classify import fingerprint

    mihomo = tmp_path / "mihomo"
    mihomo.write_bytes(b"")
    settings = Settings(
        mihomo_path=mihomo, node_retry_backoff_ms=0, max_parallel_nodes=2, max_node_attempts=1
    )
    alpha = {"name": "alpha", "type": "ss", "server": "8.8.8.8", "port": 8388, "password": "pw"}
    beta = {"name": "beta", "type": "ss", "server": "8.8.4.4", "port": 8388, "password": "pw"}
    previous = {"nodes": [
        {"fingerprint": fingerprint(proxy), "status": "ok", "checked_at": run_scan._iso_now(),
         "exit_ip": "203.0.113.9"}
        for proxy in (alpha, beta)
    ]}
    yaml_file = tmp_path / "subscription.yaml"
    yaml_file.write_bytes(SUBSCRIPTION)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"label": "l", "reuse_hours": 20, "previous": previous}))
    collectors = {"gamma": collector_factory(["fail"])}

    class RoutingCollector:
        def __init__(self, proxy_url, **_):
            self.proxy_url = proxy_url

        async def collect(self, *, node_name, **kwargs):
            inner = collectors[node_name](self.proxy_url, timeout_ms=0)
            return await inner.collect(node_name=node_name, **kwargs)

    def manager_factory(app_settings, store, workspace, *, download):
        runner = NodeRunner(
            app_settings, mihomo_factory=FakeMihomo, collector_factory=RoutingCollector
        )
        return ScanJobManager(
            app_settings, store, workspace, node_runner=runner, download=download,
            resolve_interface=lambda configured: None, resolve_family_probe=_no_family_probe,
        )

    classify_output = tmp_path / "classify.json"
    args = run_scan.parse_args(
        [
            "--request-id", "clf-partial-reuse",
            "--subscription-yaml-file", str(yaml_file),
            "--output-dir", str(tmp_path / "artifact"),
            "--run-id", "7",
            "--run-attempt", "1",
            "--profile", "classify",
            "--classify-output", str(classify_output),
            "--classify-context", str(context),
        ]
    )
    await run_scan.run(args, app_settings=settings, manager_factory=manager_factory)
    result = json.loads(classify_output.read_bytes())
    assert [node["status"] for node in result["nodes"]] == ["ok", "ok", "failed"]
    assert result["counts"]["reused"] == 2
