from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import RESULTS_DIR
from .artifact import build_export, build_manifest
from .validation import ResultStoreError, node_filename, summary, validate_node

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROGRESS_FIELDS = {
    "job_id",
    "status",
    "phase",
    "total",
    "skipped",
    "completed",
    "success_count",
    "partial_count",
    "failed_count",
    "current_node",
    "manifest_ready",
    "cleanup_confirmed",
    "updated_at",
    "error",
}


class ResultStore:
    def __init__(self, root: Path = RESULTS_DIR) -> None:
        self.root = root

    def initialize(self, job_id: str, progress: dict[str, Any]) -> None:
        job_dir = self._job_dir(job_id)
        try:
            (job_dir / "nodes").mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ResultStoreError(f"结果目录已存在：{job_id}") from exc
        self.write_progress(job_id, progress)

    def write_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        payload = {key: value for key, value in progress.items() if key in _PROGRESS_FIELDS}
        self._atomic_write(self._job_dir(job_id) / "progress.json", payload)

    def write_node(self, job_id: str, index: int, record: dict[str, Any]) -> str:
        self._validate_node(job_id, index, record)
        filename = self._node_filename(index)
        path = self._job_dir(job_id) / "nodes" / filename
        if path.exists():
            raise ResultStoreError(f"节点索引已写入：{index}")
        self._atomic_write(path, record)
        return filename

    def finalize(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        expected_total = job.get("total")
        if not isinstance(expected_total, int) or expected_total < 1:
            raise ResultStoreError("完成 manifest 前必须有合法节点总数")

        try:
            records = [self.read_node(job_id, index) for index in range(expected_total)]
        except ResultStoreError as exc:
            raise ResultStoreError(f"节点暂存文件不完整：期望 {expected_total} 个连续节点") from exc
        files = sorted((self._job_dir(job_id) / "nodes").glob("*.json"))
        if len(files) != expected_total:
            raise ResultStoreError(f"节点暂存文件不完整：期望 {expected_total}，实际 {len(files)}")

        raw = [
            (self._job_dir(job_id) / "nodes" / node_filename(index)).read_bytes()
            for index in range(expected_total)
        ]
        manifest = build_manifest(job_id, job, records, raw)
        self._atomic_write(self._job_dir(job_id) / "manifest.json", manifest)
        return manifest

    # 校验只在三处：write_node 写入时逐条、finalize 落 manifest 前整体、
    # 发布前 build_artifact 再整体一次。读回自己刚写的文件不再重复校验
    # （原先每条记录一次扫描要被校验约 8 遍）。
    def read_manifest(self, job_id: str) -> dict[str, Any]:
        manifest = self._read_json(self._job_dir(job_id) / "manifest.json")
        if manifest.get("job_id") != job_id or not isinstance(manifest.get("total"), int):
            raise ResultStoreError("manifest 结构或任务绑定无效")
        return manifest

    def read_node(self, job_id: str, index: int) -> dict[str, Any]:
        if index < 0:
            raise ResultStoreError("节点索引不能为负数")
        record = self._read_json(self._job_dir(job_id) / "nodes" / self._node_filename(index))
        if record.get("job_id") != job_id or record.get("node_index") != index:
            raise ResultStoreError("节点记录结构或身份无效")
        return record

    def read_available_summaries(
        self,
        job_id: str,
        *,
        expected_count: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(expected_count, int) or expected_count < 0:
            raise ResultStoreError("已完成节点数无效")
        node_dir = self._job_dir(job_id) / "nodes"
        files = sorted(node_dir.glob("*.json"))
        if len(files) != expected_count:
            raise ResultStoreError(f"节点暂存数量不一致：期望 {expected_count}，实际 {len(files)}")

        summaries = []
        for path in files:
            try:
                index = int(path.stem)
            except ValueError as exc:
                raise ResultStoreError(f"节点暂存文件名无效：{path.name}") from exc
            if path.name != self._node_filename(index):
                raise ResultStoreError(f"节点暂存文件名无效：{path.name}")
            summaries.append(self._summary(self.read_node(job_id, index)))
        return summaries

    def read_summaries(self, job_id: str) -> list[dict[str, Any]]:
        manifest = self.read_manifest(job_id)
        records = manifest.get("records")
        if not isinstance(records, list):
            raise ResultStoreError("manifest records 无效")
        return [entry["summary"] for entry in records]

    def export(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        manifest = self.read_manifest(job_id)
        total = manifest.get("total")
        counts = manifest.get("counts")
        if not isinstance(total, int) or not isinstance(counts, dict):
            raise ResultStoreError("manifest 完成事实无效")
        results = [self.read_node(job_id, index) for index in range(total)]
        return build_export(job_id, job, manifest, results)

    summary = staticmethod(summary)
    _summary = staticmethod(summary)
    _validate_node = staticmethod(validate_node)
    _node_filename = staticmethod(node_filename)

    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ResultStoreError("任务 ID 非法")
        return self.root / job_id

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResultStoreError(f"暂存结果不存在：{path.name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ResultStoreError(f"暂存结果不可读：{path.name}") from exc
        if not isinstance(payload, dict):
            raise ResultStoreError(f"暂存结果格式无效：{path.name}")
        return payload


result_store = ResultStore()
