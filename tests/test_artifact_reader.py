"""runner 产出的 artifact 能被浏览器端 readArtifact 读回——两端之间唯一的契约测试。"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend.app.results.artifact import build_artifact

from .conftest import JOB_ID
from .test_results import _exported, _store_with

ROOT = Path(__file__).resolve().parents[1]
READER = ROOT / "frontend-next" / "app" / "artifact" / "reader.js"

SCRIPT = """
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
const [reader, zipPath, identity] = process.argv.slice(1);
const { readArtifact } = await import(pathToFileURL(reader).href);
const bytes = await readFile(zipPath);
try {
  const { result } = await readArtifact(new Uint8Array(bytes).buffer, JSON.parse(identity));
  console.log(JSON.stringify({ ok: true, total: result.total }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, error: String(error.message) }));
}
"""

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def _artifact_zip(tmp_path, records, *, tamper=None):
    store = _store_with(tmp_path, records)
    result, status = build_artifact(_exported(store, records), JOB_ID, 7, 1, set())
    if tamper:
        result = tamper(result)
    path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("result.json", result)
        archive.writestr("status.json", status)
    return path


def _read(path, run_id=7):
    identity = json.dumps({"requestId": JOB_ID, "runId": run_id, "runAttempt": 1})
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, str(READER), str(path), identity],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def test_browser_reader_accepts_runner_artifact(tmp_path, make_success, make_failed):
    path = _artifact_zip(tmp_path, [make_success(0), make_failed(1)])
    assert _read(path) == {"ok": True, "total": 2}


def test_browser_reader_rejects_wrong_run_and_tampered_bytes(tmp_path, make_success):
    path = _artifact_zip(tmp_path, [make_success(0)])
    assert _read(path, run_id=8)["ok"] is False

    tampered = _artifact_zip(
        tmp_path / "t", [make_success(0)], tamper=lambda raw: raw.replace(b"203.0.113.1", b"203.0.113.9")
    )
    assert _read(tampered)["ok"] is False
