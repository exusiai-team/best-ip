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
        def __init__(self, proxy_url, *, timeout_ms):
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
