from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from ..async_utils import to_thread_uncancelled as _to_thread_uncancelled
from ..config import Settings
from ..mihomo import (
    MIHOMO_NOT_READY_MESSAGE,
    MihomoNotReadyError,
    MihomoStopError,
    resolve_outbound_interface,
)
from ..results.store import ResultStore, ResultStoreError
from ..subscription import (
    SubscriptionError,
    download_subscription,
    is_subscription_metadata,
    parse_subscription,
)
from .errors import (
    ScanError,
    _now,
    _safe_job_error,
    classify_error,
    live_reason,
    live_record_reason,
)
from .models import ScanProgress
from .node_runner import NodeRunner, _dns_bootstrap_candidates, _remove_work_dir


class JobNotFoundError(KeyError):
    pass


class JobNotReadyError(RuntimeError):
    pass


class JobAlreadyExistsError(RuntimeError):
    pass


_PROGRESS_WRITE_INTERVAL_SECONDS = 0.2
# 实时视图里一个节点「进行中」的几档，按先后排：并行重试时显示走得最远的那一路。
_LIVE_STAGE_RANK = {"retry": 0, "start": 1, "connect": 2, "lookup": 3}


class ScanJobManager:
    def __init__(
        self,
        app_settings: Settings,
        result_store: ResultStore,
        workspace: Path,
        *,
        node_runner: NodeRunner | None = None,
        download: Callable[..., Any] = download_subscription,
        parse: Callable[..., Any] = parse_subscription,
        resolve_interface: Callable[..., Any] = resolve_outbound_interface,
    ) -> None:
        self.settings = app_settings
        self.result_store = result_store
        self.workspace = workspace.resolve()
        self.node_runner = node_runner or NodeRunner(app_settings)
        self.download = download
        self.parse = parse
        self.resolve_interface = resolve_interface
        self._errors: dict[str, ScanError] = {}
        self._node_errors: dict[str, dict[int, ScanError]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._summaries: dict[str, dict[int, dict[str, Any]]] = {}
        self._progress_locks: dict[str, asyncio.Lock] = {}
        self._progress_last_write: dict[str, float] = {}
        # 每个节点此刻在做什么（只给实时进度回报用，不落盘、不进产物）。
        self._live: dict[str, list[dict[str, Any]]] = {}
        self._semaphore = asyncio.Semaphore(app_settings.max_parallel_jobs)
        self.node_parallelism = app_settings.max_parallel_nodes

    def create(
        self,
        subscription_url: str,
        *,
        subscription_sha256: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, str]:
        job_id = request_id or uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise ValueError("扫描请求 ID 无效")
        if job_id in self.jobs:
            raise JobAlreadyExistsError(job_id)
        if not self.settings.mihomo_path.is_file():
            raise MihomoNotReadyError(MIHOMO_NOT_READY_MESSAGE)
        self.jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "message": "等待扫描资源",
            "created_at": _now(),
            "finished_at": None,
            "total": 0,
            "skipped": 0,
            "completed": 0,
            "success_count": 0,
            "partial_count": 0,
            "failed_count": 0,
            "current_node": None,
            "manifest_ready": False,
            "cleanup_confirmed": True,
            "execution_mode": "parallel" if self.node_parallelism > 1 else "sequential",
            "error": None,
            "metrics": {
                "configured_node_parallelism": self.node_parallelism,
                "active_nodes": 0,
                "peak_active_nodes": 0,
                "wall_ms": None,
                "phase_ms": {
                    "prepare": 0,
                    "start": 0,
                    "select": 0,
                    "collect": 0,
                    "stop": 0,
                    "write": 0,
                    "finalize": 0,
                },
            },
        }
        self._summaries[job_id] = {}
        self._node_errors[job_id] = {}
        self._progress_locks[job_id] = asyncio.Lock()
        self._tasks[job_id] = asyncio.create_task(
            self._run(
                job_id,
                subscription_url,
                subscription_sha256=subscription_sha256,
            )
        )
        return {"id": job_id, "status": "queued"}

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        snapshot = copy.deepcopy(job)
        if job.get("status") == "completed" and job.get("manifest_ready"):
            try:
                snapshot["manifest"] = self.result_store.read_manifest(job_id)
                snapshot["results"] = self.result_store.read_summaries(job_id)
            except ResultStoreError as exc:
                self._errors[job_id] = classify_error(exc, phase="validate")
                snapshot["status"] = "failed"
                snapshot["manifest_ready"] = False
                snapshot["error"] = str(exc)
                snapshot["results"] = []
        else:
            completed = job.get("completed", 0)
            cached = self._summaries.get(job_id)
            if cached is not None:
                snapshot["results"] = [copy.deepcopy(cached[index]) for index in sorted(cached)]
                if len(snapshot["results"]) != completed:
                    snapshot["error"] = "节点摘要缓存与完成计数不一致"
                    snapshot["results"] = []
            elif isinstance(completed, int) and completed > 0:
                try:
                    snapshot["results"] = self.result_store.read_available_summaries(
                        job_id,
                        expected_count=completed,
                    )
                except ResultStoreError as exc:
                    snapshot["error"] = str(exc)
                    snapshot["results"] = []
            else:
                snapshot["results"] = []
        failure = self._errors.get(job_id)
        snapshot["error_details"] = asdict(failure) if failure else None
        node_errors = self._node_errors.get(job_id, {})
        for result in snapshot["results"]:
            node_error = node_errors.get(result.get("node_index"))
            if node_error is not None:
                result["error_details"] = asdict(node_error)
        return snapshot

    def get_result(self, job_id: str, index: int) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if job.get("status") != "completed" or not job.get("manifest_ready"):
            raise JobNotReadyError("扫描尚未生成 completed manifest")
        try:
            manifest = self.result_store.read_manifest(job_id)
            total = manifest.get("total")
            if not isinstance(total, int) or index < 0 or index >= total:
                raise JobNotFoundError(job_id)
            return self.result_store.read_node(job_id, index)
        except ResultStoreError as exc:
            raise JobNotFoundError(job_id) from exc

    def export(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if job.get("status") != "completed" or not job.get("manifest_ready"):
            raise JobNotReadyError("扫描尚未生成 completed manifest")
        try:
            return self.result_store.export(job_id, copy.deepcopy(job))
        except ResultStoreError as exc:
            raise JobNotReadyError(str(exc)) from exc

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        task = self._tasks.get(job_id)
        if task and not task.done():
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if job.get("status") == "queued":
            job["status"] = "cancelled"
            job["message"] = "扫描已取消"
            job["cleanup_confirmed"] = True
            job["finished_at"] = _now()
            await self._safe_write_progress(job)
        if (
            job.get("status") in {"completed", "failed", "cancelled"}
            and job.get("cleanup_confirmed") is not True
        ):
            raise JobNotReadyError("任务已终止，但 Mihomo 清理未确认")
        return await _to_thread_uncancelled(self.get, job_id)

    async def wait(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise JobNotFoundError(job_id)
        task = self._tasks.get(job_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        return await _to_thread_uncancelled(self.get, job_id)

    def get_error(self, job_id: str) -> ScanError | None:
        if job_id not in self.jobs:
            raise JobNotFoundError(job_id)
        return self._errors.get(job_id)

    def live_nodes(self, job_id: str) -> list[dict[str, Any]] | None:
        """每个节点此刻的状态（订阅顺序）：等待 / 进行到哪一档 / 终态，以及已用时与原因。

        进行中的节点给的是「到此刻为止」的用时。订阅还没解析完时返回 None。
        """

        live = self._live.get(job_id)
        if live is None:
            return None
        now = perf_counter()
        snapshot = []
        for entry in live:
            started = entry["started"]
            elapsed = entry["elapsed_ms"]
            if elapsed is None:
                elapsed = 0 if started is None else round((now - started) * 1000)
            snapshot.append(
                {
                    "name": entry["name"],
                    "type": entry["type"],
                    "state": entry["state"],
                    "attempt": entry["attempt"],
                    "elapsed_ms": elapsed,
                    "reason": entry["reason"],
                }
            )
        return snapshot

    async def shutdown(self) -> None:
        active = [task for task in self._tasks.values() if not task.done()]
        for task in active:
            if not task.cancelling():
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for job in self.jobs.values():
            if job.get("status") == "queued":
                job["status"] = "cancelled"
                job["message"] = "扫描已取消"
                job["cleanup_confirmed"] = True
                job["finished_at"] = _now()
                await self._safe_write_progress(job)
        if any(job.get("cleanup_confirmed") is False for job in self.jobs.values()):
            raise JobNotReadyError("任务已终止，但 Mihomo 清理未确认")

    async def _run(
        self,
        job_id: str,
        subscription_url: str,
        *,
        subscription_sha256: str | None = None,
    ) -> None:
        job = self.jobs[job_id]
        wall_started = perf_counter()
        work_dir = self.workspace / job_id
        failure_phase = "prepare"
        try:
            async with self._semaphore:
                job["status"] = "preparing"
                job["message"] = "正在安全下载并解析订阅"
                prepare_started = perf_counter()
                try:
                    content = await self.download(
                        subscription_url,
                        max_bytes=self.settings.subscription_max_bytes,
                        timeout_seconds=self.settings.subscription_timeout_seconds,
                    )
                    if (
                        subscription_sha256 is not None
                        and hashlib.sha256(content).hexdigest() != subscription_sha256
                    ):
                        raise SubscriptionError("订阅内容与请求快照不一致")
                    outbound_interface = await _to_thread_uncancelled(
                        self.resolve_interface,
                        self.settings.outbound_interface,
                    )
                    parsed_proxies = await _to_thread_uncancelled(
                        self.parse,
                        content,
                        max_nodes=self.settings.max_nodes,
                    )
                    proxies = [
                        proxy for proxy in parsed_proxies if not is_subscription_metadata(proxy)
                    ]
                    dns_bootstrap_candidates = _dns_bootstrap_candidates(proxies)
                    job["skipped"] = len(parsed_proxies) - len(proxies)
                    if not proxies:
                        raise SubscriptionError("订阅中没有可检测的代理节点")
                    job["total"] = len(proxies)
                    self._live[job_id] = [_live_entry(proxy) for proxy in proxies]
                    await _to_thread_uncancelled(
                        self.result_store.initialize,
                        job_id,
                        _progress(job),
                    )
                    self._progress_last_write[job_id] = asyncio.get_running_loop().time()
                finally:
                    _record_phase(job, "prepare", prepare_started)

                job["status"] = "running"
                job["message"] = f"正在并行检测 {len(proxies)} 个节点"
                await self._write_progress(job)
                node_queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
                for index, proxy in enumerate(proxies):
                    node_queue.put_nowait((index, proxy))

                async def node_worker() -> None:
                    while True:
                        try:
                            index, proxy = node_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        try:
                            await self._scan_node(
                                job,
                                work_dir,
                                proxies,
                                proxy,
                                index,
                                dns_bootstrap_candidates,
                                outbound_interface,
                            )
                        finally:
                            node_queue.task_done()

                worker_count = min(self.node_parallelism, len(proxies))
                failure_phase = "scan"
                node_tasks = [asyncio.create_task(node_worker()) for _ in range(worker_count)]
                job["cleanup_confirmed"] = False
                try:
                    await asyncio.gather(*node_tasks)
                except BaseException as exc:
                    for task in node_tasks:
                        if not task.done() and not task.cancelling():
                            task.cancel()
                    outcomes = await asyncio.gather(
                        *node_tasks,
                        return_exceptions=True,
                    )
                    if any(isinstance(outcome, MihomoStopError) for outcome in outcomes):
                        job["cleanup_confirmed"] = False
                        raise MihomoStopError("Mihomo 子进程清理未确认") from exc
                    job["cleanup_confirmed"] = True
                    raise
                job["cleanup_confirmed"] = True
                if not await _to_thread_uncancelled(_remove_work_dir, work_dir, self.workspace):
                    job["cleanup_confirmed"] = False
                    raise MihomoStopError("Mihomo 工作目录清理失败")

                job["current_node"] = None
                job["finished_at"] = _now()
                finalize_started = perf_counter()
                failure_phase = "validate"
                try:
                    await _to_thread_uncancelled(self.result_store.finalize, job_id, job)
                finally:
                    _record_phase(job, "finalize", finalize_started)
                job["status"] = "completed"
                job["manifest_ready"] = True
                suffix = f"，已跳过 {job['skipped']} 个订阅信息项" if job["skipped"] else ""
                job["message"] = f"扫描完成，共 {len(proxies)} 个节点{suffix}"
                await self._safe_write_progress(job)
        except asyncio.CancelledError:
            work_dir_removed = bool(job.get("cleanup_confirmed")) and await _to_thread_uncancelled(
                _remove_work_dir, work_dir, self.workspace
            )
            job["cleanup_confirmed"] = bool(job.get("cleanup_confirmed") and work_dir_removed)
            job["status"] = "cancelled" if job["cleanup_confirmed"] else "failed"
            job["message"] = "扫描已取消" if job["cleanup_confirmed"] else "扫描取消后的清理未确认"
            job["error"] = None if job["cleanup_confirmed"] else "Mihomo 进程或工作目录清理未确认"
            job["current_node"] = None
            job["finished_at"] = _now()
            if not job["cleanup_confirmed"]:
                self._errors[job_id] = classify_error(MihomoStopError())
            await self._safe_write_progress(job)
        except Exception as exc:
            self._errors.setdefault(job_id, classify_error(exc, phase=failure_phase))
            work_dir_removed = bool(job.get("cleanup_confirmed")) and await _to_thread_uncancelled(
                _remove_work_dir, work_dir, self.workspace
            )
            job["cleanup_confirmed"] = bool(job.get("cleanup_confirmed") and work_dir_removed)
            job["status"] = "failed"
            job["message"] = "扫描任务失败"
            job["current_node"] = None
            job["finished_at"] = _now()
            job["error"] = (
                _safe_job_error(exc)
                if job["cleanup_confirmed"]
                else "Mihomo 进程或工作目录清理未确认"
            )
            if not job["cleanup_confirmed"]:
                self._errors[job_id] = classify_error(MihomoStopError())
            await self._safe_write_progress(job)
        finally:
            if work_dir.exists():
                job["cleanup_confirmed"] = False
            job["finished_at"] = job.get("finished_at") or _now()
            metrics = job.get("metrics")
            if isinstance(metrics, dict):
                metrics["wall_ms"] = round((perf_counter() - wall_started) * 1000)

    async def _scan_node(
        self,
        job: dict[str, Any],
        work_dir: Path,
        proxies: list[dict[str, Any]],
        proxy: dict[str, Any],
        index: int,
        dns_bootstrap_candidates: list[str],
        outbound_interface: str | None,
    ) -> None:
        metrics = job.get("metrics")
        if isinstance(metrics, dict):
            metrics["active_nodes"] = int(metrics.get("active_nodes") or 0) + 1
            metrics["peak_active_nodes"] = max(
                int(metrics.get("peak_active_nodes") or 0),
                metrics["active_nodes"],
            )
        live = self._live.get(job["id"])
        entry = live[index] if live is not None and index < len(live) else None
        node_started = perf_counter()
        try:

            def on_stage(stage: str, attempt: int, reason: str | None) -> None:
                if stage == "start":
                    job["message"] = (
                        f"正在并行检测 {index + 1}/{job['total']}：{proxy['name']}，"
                        f"尝试 {attempt}/{self.settings.max_node_attempts}"
                    )
                if entry is not None:
                    _advance_live(entry, stage, attempt, reason, node_started)

            outcome = await self.node_runner.run(
                job_id=job["id"],
                work_dir=work_dir,
                proxies=proxies,
                proxy=proxy,
                index=index,
                dns_bootstrap_candidates=dns_bootstrap_candidates,
                outbound_interface=outbound_interface,
                on_stage=on_stage,
            )
            result = outcome.record
            if outcome.error is not None:
                self._node_errors.setdefault(job["id"], {})[index] = outcome.error
            if isinstance(metrics, dict):
                for phase, elapsed in outcome.phase_ms.items():
                    metrics["phase_ms"][phase] += elapsed
            phase_started = perf_counter()
            try:
                await _to_thread_uncancelled(
                    self.result_store.write_node,
                    job["id"],
                    index,
                    result,
                )
            except Exception as exc:
                self._errors[job["id"]] = classify_error(exc, phase="write")
                raise
            finally:
                _record_phase(job, "write", phase_started)
            self._summaries.setdefault(job["id"], {})[index] = self.result_store.summary(result)
            # 终态与计数在同一段同步代码里落定：进度回报读到的「各状态节点数」与计数永远一致。
            if entry is not None:
                entry["state"] = str(result.get("status") or "failed")
                entry["elapsed_ms"] = round((perf_counter() - node_started) * 1000)
                entry["reason"] = live_record_reason(result)
            _count_result(job, result)
            job["completed"] += 1
            await self._write_progress(job)
        finally:
            if isinstance(metrics, dict):
                metrics["active_nodes"] = max(
                    0,
                    int(metrics.get("active_nodes") or 0) - 1,
                )

    async def _write_progress(self, job: dict[str, Any], *, force: bool = False) -> None:
        job_id = job["id"]
        lock = self._progress_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            last_write = self._progress_last_write.get(job_id)
            if (
                not force
                and last_write is not None
                and now - last_write < _PROGRESS_WRITE_INTERVAL_SECONDS
            ):
                return
            await _to_thread_uncancelled(self.result_store.write_progress, job_id, _progress(job))
            self._progress_last_write[job_id] = asyncio.get_running_loop().time()

    async def _safe_write_progress(self, job: dict[str, Any]) -> None:
        try:
            if (self.result_store.root / job["id"]).exists():
                await self._write_progress(job, force=True)
        except (OSError, ResultStoreError):
            pass


def _record_phase(job: dict[str, Any], phase: str, started: float) -> None:
    metrics = job.get("metrics")
    phase_ms = metrics.get("phase_ms") if isinstance(metrics, dict) else None
    if not isinstance(phase_ms, dict) or phase not in phase_ms:
        return
    phase_ms[phase] = int(phase_ms[phase]) + round((perf_counter() - started) * 1000)


def _progress(job: dict[str, Any]) -> ScanProgress:
    return {
        "job_id": job["id"],
        "status": job.get("status"),
        "phase": job.get("message"),
        "total": job.get("total", 0),
        "skipped": job.get("skipped", 0),
        "completed": job.get("completed", 0),
        "success_count": job.get("success_count", 0),
        "partial_count": job.get("partial_count", 0),
        "failed_count": job.get("failed_count", 0),
        "current_node": job.get("current_node"),
        "manifest_ready": job.get("manifest_ready", False),
        "cleanup_confirmed": job.get("cleanup_confirmed", True),
        "updated_at": _now(),
        "error": job.get("error"),
    }


def _live_entry(proxy: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(proxy.get("name") or ""),
        "type": str(proxy.get("type") or ""),
        "state": "wait",
        "attempt": 0,
        "started": None,
        "elapsed_ms": None,
        "reason": None,
    }


def _advance_live(
    entry: dict[str, Any], stage: str, attempt: int, reason: str | None, started: float
) -> None:
    """进行中的一档。并行重试的几路各自报，显示走得最远的那一路；进入重试时从头算。"""

    if stage not in _LIVE_STAGE_RANK or entry["elapsed_ms"] is not None:
        return
    if entry["started"] is None:
        entry["started"] = started
    if stage == "retry":
        entry["state"] = "retry"
        # 上一次失败的原文可能夹带主机名或地址：只留白名单里的那句分类文案。
        entry["reason"] = live_reason(reason)
    elif _LIVE_STAGE_RANK[stage] >= _LIVE_STAGE_RANK.get(entry["state"], -1):
        entry["state"] = stage
    entry["attempt"] = max(int(entry["attempt"]), attempt)


def _count_result(job: dict[str, Any], result: dict[str, Any]) -> None:
    status = result.get("status")
    if status == "success":
        job["success_count"] += 1
    elif status == "partial":
        job["partial_count"] += 1
    else:
        job["failed_count"] += 1
