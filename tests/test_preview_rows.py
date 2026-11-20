"""扫描中的表格预览：runner 的预览行 → Worker 校验 → 页面 toRows，三段走同一份字段。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app.scan.reporter import live_nodes, live_rows, preview_row

from .conftest import alt_exit

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = """
import { pathToFileURL } from "node:url";
const [root, payload] = process.argv.slice(1);
const load = (path) => import(pathToFileURL(`${root}/${path}`).href);
const { normalizeReport } = await load("worker/progress.js");
const { normalizeProgress } = await load("frontend-next/app/progress.js");
const { toRows } = await load("frontend-next/app/records.js");
const report = normalizeReport(JSON.parse(payload));
const progress = normalizeProgress(report);
const rows = progress.rows ? toRows({ results: progress.rows }) : null;
const stored = report.rows?.length ?? null;
console.log(JSON.stringify({ stored, nodes: report.nodes.length, rows }));
"""

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def _record(make_success, index: int) -> dict:
    record = make_success(index)
    record.update(
        country_code="SG", city="Singapore", isp="Amazon.com, Inc.", asn=16509, rdns="ec2.example",
        usage_type="hosting", is_residential=False, is_native=False, native_status="广播 IP (US)",
        ipx_score=55, ipx_grade="一般", ipx_report_url="https://ipx.al/203.0.113.1",
        network_source={"exit_ip": "ipx", "geo": "ipx", "isp": "ipx", "kind": "ipx",
                        "native": "ipx"},
    )
    record["requests"]["ipure"]["error_type"] = "NotIndexed"
    record.update(score=None, ipure_scores=None)
    return record


def _payload(snapshot: list[dict], redact: set[str] | None = None, rows=None) -> str:
    nodes = live_nodes(snapshot, redact or set())
    states = [node["s"] for node in nodes]
    counts = {state: states.count(state) for state in ("success", "partial", "failed")}
    return json.dumps({
        "request_id": "req-test-0001", "run_id": 7, "run_attempt": 1, "phase": "scanning",
        "total": len(nodes), "completed": sum(counts.values()), **counts, "nodes": nodes,
        "rows": live_rows(snapshot, redact or set()) if rows is None else rows,
    }, ensure_ascii=False)


def _run(payload: str) -> dict:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, str(ROOT), payload],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(completed.stdout)


def _snapshot(make_success) -> list[dict]:
    record = _record(make_success, 0)
    return [
        {"name": "node-0", "type": "ss", "state": "success", "attempt": 1, "elapsed_ms": 900,
         "reason": None, "row": preview_row(record)},
        {"name": "node-1", "type": "ss", "state": "lookup", "attempt": 1, "elapsed_ms": 400,
         "reason": None, "row": None},
    ]


def test_preview_row_reaches_the_table(make_success):
    result = _run(_payload(_snapshot(make_success)))
    assert (result["stored"], result["nodes"]) == (1, 2)
    [row] = result["rows"]
    assert (row["exit_ip"], row["country_code"], row["asn"]) == ("203.0.113.1", "SG", 16509)
    assert row["sources"]["geo"] == "ipx"
    assert row["ipx_score"] == 55
    assert row["score_note"] == "IPure 未收录该 IP"


def test_invalid_rows_are_dropped_but_progress_is_kept(make_success):
    snapshot = _snapshot(make_success)
    bad = [{**live_rows(snapshot, set())[0], "requests": {"ipqs": {"data": "raw"}}}]
    result = _run(_payload(snapshot, rows=bad))
    assert (result["stored"], result["nodes"], result["rows"]) == (None, 2, None)


def test_row_for_a_node_still_running_is_rejected(make_success):
    snapshot = _snapshot(make_success)
    row = {**live_rows(snapshot, set())[0], "node_index": 1}
    assert _run(_payload(snapshot, rows=[row]))["stored"] is None


def test_rows_with_subscription_secrets_are_not_sent(make_success):
    snapshot = _snapshot(make_success)
    snapshot[0]["row"]["isp"] = "secret-token-value"
    assert live_rows(snapshot, {"secret-token-value"}) == []


def test_failed_nodes_have_no_preview(make_failed):
    assert preview_row(make_failed(0)) is None


TO_ROWS = """
import { pathToFileURL } from "node:url";
const [root, payload] = process.argv.slice(1);
const { toRows } = await import(pathToFileURL(`${root}/frontend-next/app/records.js`).href);
console.log(JSON.stringify(toRows(JSON.parse(payload))));
"""


def _to_rows(results: list[dict]) -> list[dict]:
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", TO_ROWS, str(ROOT),
         json.dumps({"results": results}, ensure_ascii=False)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(completed.stdout)


def test_alt_exit_becomes_a_child_row_of_its_node(make_success, make_failed):
    dual = _record(make_success, 0)
    dual["alt_exit"] = {**alt_exit("2001:db8::7"), "country_code": "TW", "ipx_score": 95}
    rows = _to_rows([dual, _record(make_success, 1), make_failed(2)])
    # 节点数不变：第二行挂在节点行上，不是独立的一行。
    assert len(rows) == 3
    first, single, failed = rows
    assert (first["exit_ip"], first["family"]) == ("203.0.113.1", 4)
    child = first["alt"]
    assert (child["exit_ip"], child["family"], child["child"]) == ("2001:db8::7", 6, True)
    assert (child["node"], child["country_code"], child["ipx_score"]) == ("node-0", "TW", 95)
    assert child["alt"] is None
    assert (single["family"], single["alt"]) == (None, None)
    assert (failed["family"], failed["alt"]) == (None, None)


def test_alt_exit_of_the_same_family_is_not_drawn(make_success):
    dual = _record(make_success, 0)
    dual["alt_exit"] = alt_exit("198.51.100.7")
    [row] = _to_rows([dual])
    assert (row["family"], row["alt"]) == (None, None)
