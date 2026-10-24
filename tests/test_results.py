from __future__ import annotations

import json

import pytest

from backend.app.results.artifact import build_artifact
from backend.app.results.store import ResultStore
from backend.app.results.validation import ResultStoreError, validate_node

from .conftest import JOB_ID


def _job(total: int) -> dict:
    return {
        "id": JOB_ID,
        "status": "completed",
        "total": total,
        "completed": total,
        "skipped": 0,
        "created_at": "2026-10-24T00:00:00Z",
        "finished_at": "2026-10-24T00:00:05Z",
        "execution_mode": "parallel",
        "cleanup_confirmed": True,
        "manifest_ready": True,
    }


def _store_with(tmp_path, records):
    store = ResultStore(tmp_path / "results")
    store.initialize(JOB_ID, {"job_id": JOB_ID, "status": "running"})
    for index, record in enumerate(records):
        store.write_node(JOB_ID, index, record)
    return store


def _exported(store, records):
    job = _job(len(records))
    store.finalize(JOB_ID, job)
    exported = store.export(JOB_ID, job)
    counts = exported["manifest"]["counts"]
    exported.update({f"{key}_count": counts[key] for key in ("success", "partial", "failed")})
    return exported


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["ipure_scores"].update(ai=101), "取值无效"),
        (lambda r: r["ipure_scores"].update(ai=-2), "取值无效"),
        (lambda r: r.update(score=70), "不一致"),
        (lambda r: r["completeness"]["checks"].update(lookup_matches_trace=False), "证据无效"),
        (lambda r: r["proxy_evidence"].update(proxy_url="http://10.0.0.1:1"), "证据无效"),
        (lambda r: r.update(exit_ip="not-an-ip"), "IP"),
        (lambda r: r.update(unexpected=1), "字段集合"),
        (lambda r: r.update(node_index=5), "身份"),
    ],
)
def test_validate_node_rejects_broken_success_records(make_success, mutate, message):
    record = make_success(0)
    mutate(record)
    with pytest.raises(ResultStoreError, match=message):
        validate_node(JOB_ID, 0, record)


def test_failed_record_must_not_carry_exit_ip(make_failed):
    record = make_failed(0)
    validate_node(JOB_ID, 0, record)
    record["exit_ip"] = "203.0.113.9"
    with pytest.raises(ResultStoreError, match="出口 IP"):
        validate_node(JOB_ID, 0, record)


def test_store_rejects_invalid_record_at_write_time(tmp_path, make_success):
    store = ResultStore(tmp_path / "results")
    store.initialize(JOB_ID, {"job_id": JOB_ID})
    record = make_success(0)
    record["status"] = "unknown"
    with pytest.raises(ResultStoreError):
        store.write_node(JOB_ID, 0, record)
    store.write_node(JOB_ID, 0, make_success(0))
    with pytest.raises(ResultStoreError, match="已写入"):
        store.write_node(JOB_ID, 0, make_success(0))


def test_finalize_requires_every_node(tmp_path, make_success):
    store = _store_with(tmp_path, [make_success(0)])
    with pytest.raises(ResultStoreError, match="不完整"):
        store.finalize(JOB_ID, _job(2))


def test_manifest_counts_and_artifact_round_trip(tmp_path, make_success, make_failed):
    records = [make_success(0), make_failed(1), make_success(2)]
    store = _store_with(tmp_path, records)
    exported = _exported(store, records)
    assert exported["manifest"]["counts"] == {
        "success": 2,
        "partial": 0,
        "failed": 1,
        "complete": 2,
    }

    result_bytes, status_bytes = build_artifact(exported, JOB_ID, 42, 1, {"secret-token"})
    status = json.loads(status_bytes)
    assert status["result_sha256"]
    assert status["usable"] is False
    assert json.loads(result_bytes)["results"][1]["status"] == "failed"


def test_artifact_refuses_subscription_secrets(tmp_path, make_success):
    record = make_success(0, name="leak-secret-token")
    store = _store_with(tmp_path, [record])
    exported = _exported(store, [record])
    with pytest.raises(ResultStoreError, match="凭据"):
        build_artifact(exported, JOB_ID, 42, 1, {"secret-token"})


def test_artifact_detects_tampered_record(tmp_path, make_success):
    records = [make_success(0)]
    store = _store_with(tmp_path, records)
    exported = _exported(store, records)
    exported["results"][0]["exit_ip"] = "198.51.100.7"
    with pytest.raises(ResultStoreError):
        build_artifact(exported, JOB_ID, 42, 1, set())
