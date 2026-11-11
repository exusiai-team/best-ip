from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

ERRORS = {
    "input_invalid": (2, "扫描输入或运行身份无效"),
    "environment_not_ready": (3, "扫描运行环境未就绪"),
    "scan_unavailable": (4, "扫描没有可用节点结果"),
    "result_invalid": (5, "扫描产物校验失败"),
    "scan_timeout": (6, "扫描超过总时间预算，已请求取消"),
    "cleanup_failed": (7, "扫描资源清理未确认，未发布产物"),
    "output_failed": (8, "无法安全发布扫描产物"),
    "scan_cancelled": (130, "扫描已取消，未发布产物"),
}


class ScanCLIError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        self.exit_code, message = ERRORS[code]
        super().__init__(message)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScanCLIError("input_invalid")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeArgumentParser(description="Run the shared scan core without an HTTP server")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--subscription-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    parser.add_argument("--local-test", action="store_true")
    parser.add_argument("--timeout", type=float, default=1500)
    return parser.parse_args(argv)


def _validate_args(args: argparse.Namespace) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.request_id)
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise ScanCLIError("input_invalid")
    if args.local_test and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise ScanCLIError("input_invalid")
    for name, environment in (("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT")):
        explicit = getattr(args, name)
        raw = os.environ.get(environment)
        try:
            inherited = int(raw) if raw is not None else None
        except ValueError as exc:
            raise ScanCLIError("input_invalid") from exc
        if explicit is not None and inherited is not None and explicit != inherited:
            raise ScanCLIError("input_invalid")
        value = explicit if explicit is not None else inherited
        if value is None and args.local_test:
            value = 1
        if type(value) is not int or value < 1:
            raise ScanCLIError("input_invalid")
        setattr(args, name, value)


def _read_subscription_url(path: Path) -> str:
    try:
        with path.open("rb") as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError
        url = raw.decode("utf-8").strip()
        parsed = urlsplit(url)
        if (
            not 8 <= len(url) <= 4096
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in url)
        ):
            raise ValueError
        _ = parsed.port
        return url
    except (OSError, UnicodeError, ValueError) as exc:
        raise ScanCLIError("input_invalid") from exc


def _remove_owned(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path or not path.is_absolute():
        raise ScanCLIError("cleanup_failed")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ScanCLIError("cleanup_failed") from exc


async def _shutdown(manager: Any, job_id: str | None, cancel: bool) -> None:
    failed = False
    if cancel and job_id is not None:
        try:
            await manager.cancel(job_id)
        except Exception:
            failed = True
    try:
        await manager.shutdown()
    except Exception:
        failed = True
    if failed:
        raise ScanCLIError("cleanup_failed")


async def _confirmed_shutdown(manager: Any, job_id: str | None, cancel: bool) -> None:
    operation = asyncio.create_task(_shutdown(manager, job_id, cancel))
    interrupted = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            interrupted = True
    operation.result()
    if interrupted:
        raise asyncio.CancelledError


def _publish(output: Path, result: bytes, status: bytes) -> None:
    staging = None
    try:
        if output.exists() or output.is_symlink():
            raise ScanCLIError("output_failed")
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".best-ip-artifact-", dir=output.parent)).resolve()
        (staging / "result.json").write_bytes(result)
        (staging / "status.json").write_bytes(status)
        if output.exists() or output.is_symlink():
            raise ScanCLIError("output_failed")
        staging.rename(output)
        staging = None
    except OSError as exc:
        raise ScanCLIError("output_failed") from exc
    finally:
        if staging is not None:
            _remove_owned(staging)


def _progress_reporter(
    args: argparse.Namespace, relay_url: str, relay_token: str, factory: Any
) -> Any:
    """进度回报与订阅中继同属一个 Worker、同一张领取订阅时拿到的 runner 令牌；
    没配中继（本地）就不回报。"""

    if not relay_url:
        return None
    try:
        from backend.app.scan.reporter import ProgressReporter, progress_url_from_relay
    except Exception as exc:
        raise ScanCLIError("environment_not_ready") from exc

    factory = factory or ProgressReporter
    return factory(
        progress_url_from_relay(relay_url),
        relay_token,
        request_id=args.request_id,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )


async def _stop_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _relay_settings() -> tuple[str, str]:
    """订阅中继配置。两半要么都给，要么都不给；只给一半按配置错误处理，
    避免 runner 直连被订阅主机拒绝时静默退化。"""

    url = os.environ.get("BEST_IP_SUBSCRIPTION_RELAY_URL", "").strip()
    token = os.environ.get("BEST_IP_SUBSCRIPTION_RELAY_TOKEN", "").strip()
    if bool(url) != bool(token):
        raise ValueError("订阅中继配置不完整")
    return (url, token)


async def run(
    args: argparse.Namespace,
    *,
    app_settings: Any = None,
    manager_factory: Any = None,
    download: Any = None,
    reporter_factory: Any = None,
) -> None:
    _validate_args(args)
    url = _read_subscription_url(args.subscription_file)
    output = args.output_dir.absolute()
    if output.exists() or output.is_symlink():
        raise ScanCLIError("output_failed")
    try:
        from backend.app.config import settings
        from backend.app.scan.jobs import ScanJobManager
        from backend.app.subscription import SubscriptionRelay, download_subscription
    except Exception as exc:
        raise ScanCLIError("environment_not_ready") from exc
    app_settings = app_settings or settings
    manager_factory = manager_factory or ScanJobManager
    download = download or download_subscription
    try:
        relay_url, relay_token = _relay_settings()
        relay = SubscriptionRelay(relay_url, relay_token) if relay_url else None
        reporter = _progress_reporter(args, relay_url, relay_token, reporter_factory)
    except ValueError as exc:
        raise ScanCLIError("environment_not_ready") from exc
    if reporter is None:
        await _run_scan(args, url, output, app_settings, manager_factory, download, relay, None)
        return
    # 进度回报是尽力而为：开关连接、发送失败都不影响扫描本身，最后只留一行计数作证据。
    async with reporter:
        try:
            await _run_scan(
                args, url, output, app_settings, manager_factory, download, relay, reporter
            )
        finally:
            print(
                f"progress_reports: sent={reporter.sent} failed={reporter.failed}",
                file=sys.stderr,
            )


async def _run_scan(
    args: argparse.Namespace,
    url: str,
    output: Path,
    app_settings: Any,
    manager_factory: Any,
    download: Any,
    relay: Any,
    reporter: Any,
) -> None:
    try:
        from backend.app.mihomo import MihomoNotReadyError
        from backend.app.results.artifact import (
            build_artifact,
            credential_values,
            subscription_url_values,
        )
        from backend.app.results.store import ResultStore, ResultStoreError
        from backend.app.scan.jobs import JobNotReadyError
        from backend.app.scan.reporter import job_counts, live_nodes, live_rows
        from backend.app.subscription import (
            SubscriptionError,
            SubscriptionSource,
            subscription_failure_reason,
        )
    except Exception as exc:
        raise ScanCLIError("environment_not_ready") from exc

    owned = None
    manager = None
    job_id = None
    completed = False
    final_counts = None
    final_nodes = None
    final_rows = None
    first_report: asyncio.Task[Any] | None = None
    follow_task: asyncio.Task[Any] | None = None
    source = SubscriptionSource()
    if reporter is not None:
        # 不等这一条：订阅下载不该排在一次进度上报后面。它和后面的跟报并发也无妨——
        # Worker 只收「阶段不倒退」的报告。
        first_report = asyncio.create_task(reporter.report("subscription"))
    try:
        async with asyncio.timeout(args.timeout):
            download_started = perf_counter()
            try:
                content = await download(
                    url,
                    max_bytes=app_settings.subscription_max_bytes,
                    timeout_seconds=min(app_settings.subscription_timeout_seconds, args.timeout),
                    relay=relay,
                    source=source,
                )
            except (SubscriptionError, ValueError) as exc:
                # 失败时也得留下「先撞过直连」的痕迹：不然只看得到中继那次的码，
                # 「两个出口都要看」就成了口头约定。此处不报 subscription_source——
                # 回退已经把 value 置成 relay，但中继并没有应答。
                if source.fallback_reason is not None:
                    print(
                        f"subscription_fallback_reason: {source.fallback_reason}",
                        file=sys.stderr,
                    )
                print(
                    f"subscription_fetch_reason: {subscription_failure_reason(exc)}",
                    file=sys.stderr,
                )
                raise ScanCLIError("input_invalid") from exc
            # value 只由 download_subscription 写成 direct/relay；替身不回写时如实报 unknown。
            egress = source.value if source.value in {"direct", "relay"} else "unknown"
            download_ms = round((perf_counter() - download_started) * 1000)
            print(f"subscription_source: {egress}", file=sys.stderr)
            if source.fallback_reason is not None:
                print(
                    f"subscription_fallback_reason: {source.fallback_reason}",
                    file=sys.stderr,
                )
            if not isinstance(content, bytes) or len(content) > app_settings.subscription_max_bytes:
                raise ScanCLIError("input_invalid")
            # 与产物同一份「不许出现」的值：实时进度里的节点名含有其中任何一个就换成「节点 N」。
            forbidden = {url, *credential_values(content), *subscription_url_values(url)}
            if relay is not None:
                forbidden.add(relay.token)

            async def snapshot_loader(
                requested_url: str, *, max_bytes: int, timeout_seconds: float
            ) -> bytes:
                if requested_url != url or len(content) > max_bytes or timeout_seconds <= 0:
                    raise SubscriptionError("订阅快照无效")
                return content

            owned = Path(tempfile.mkdtemp(prefix="best-ip-scan-")).resolve()
            manager = manager_factory(
                app_settings,
                ResultStore(owned / "results"),
                owned / "jobs",
                download=snapshot_loader,
            )
            manager.create(
                url,
                subscription_sha256=hashlib.sha256(content).hexdigest(),
                request_id=args.request_id,
            )
            job_id = args.request_id
            # 逐节点的实时状态是可选能力：没有它的 manager（替身）照样只报计数。
            read_live = getattr(manager, "live_nodes", None)

            def read_nodes() -> Any:
                return read_live(job_id) if callable(read_live) else None

            if reporter is not None:
                follow_task = asyncio.create_task(
                    reporter.follow(lambda: manager.jobs.get(job_id), read_nodes, forbidden)
                )
            job = await manager.wait(job_id)
            await _stop_task(follow_task)
            follow_task = None
            final_counts = job_counts(manager.jobs.get(job_id))
            final_snapshot = read_nodes()
            final_nodes = live_nodes(final_snapshot, forbidden)
            final_rows = live_rows(final_snapshot, forbidden)
            print(
                _timing_summary(
                    download_ms, job, final_nodes, _read_records(manager, job_id, job)
                ),
                file=sys.stderr,
            )
            if reporter is not None and job.get("status") == "completed":
                await reporter.report("packaging", final_counts, final_nodes, final_rows)
            if job.get("cleanup_confirmed") is not True:
                raise ScanCLIError("cleanup_failed")
            if job.get("status") != "completed":
                failure = manager.get_error(job_id)
                code = failure.code if failure is not None else None
                if code == "subscription_invalid":
                    code = "input_invalid"
                if code not in ERRORS:
                    code = (
                        "scan_cancelled"
                        if job.get("status") == "cancelled"
                        else "scan_unavailable"
                    )
                print(f"scan_failure_code: {code}", file=sys.stderr)
                raise ScanCLIError(code)
            if job.get("manifest_ready") is not True:
                raise ScanCLIError("result_invalid")
            exported = manager.export(job_id)
            result, status = build_artifact(
                exported, job_id, args.run_id, args.run_attempt, forbidden
            )
            counts = exported["manifest"]["counts"]
            if counts["success"] + counts["partial"] < 1:
                raise ScanCLIError("scan_unavailable")
            completed = True
    except TimeoutError as exc:
        raise ScanCLIError("scan_timeout") from exc
    except MihomoNotReadyError as exc:
        raise ScanCLIError("environment_not_ready") from exc
    except (ResultStoreError, JobNotReadyError) as exc:
        raise ScanCLIError("result_invalid") from exc
    except ScanCLIError:
        raise
    except Exception as exc:
        raise ScanCLIError("scan_unavailable") from exc
    finally:
        await _stop_task(first_report)
        await _stop_task(follow_task)
        if manager is not None:
            await _confirmed_shutdown(manager, job_id, not completed)
        if owned is not None:
            _remove_owned(owned)
    _publish(output, result, status)
    if reporter is not None:
        await reporter.report("done", final_counts, final_nodes, final_rows)


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


def _read_records(manager: Any, job_id: str, job: Any) -> list[dict[str, Any]]:
    """完成的任务读回全部节点记录，只给 scan_timing 统计用；读不到就不统计。"""

    store = getattr(manager, "result_store", None)
    total = job.get("total") if isinstance(job, dict) else None
    if store is None or job.get("status") != "completed" or not isinstance(total, int):
        return []
    try:
        return [store.read_node(job_id, index) for index in range(total)]
    except Exception:
        return []


def _source_timing(records: list[dict[str, Any]]) -> list[str]:
    """逐源 p50/p90/max、直连兜底次数与出口 IP 重复度；只有数字，不含 IP 本身。

    产物 1 天后过期，事后定位长尾在哪个上游、是否值得按出口 IP 去重，都只能靠这一行。
    """

    if not records:
        return []
    from backend.app.sources.registry import SOURCES

    timings: list[str] = []
    fallbacks: list[str] = []
    shared: list[str] = []
    # 注册表的顺序即输出顺序。
    for name in (source.name for source in SOURCES):
        requests = [
            record["requests"][name]
            for record in records
            if isinstance(record.get("requests"), dict)
            and isinstance(record["requests"].get(name), dict)
            and record["requests"][name].get("attempted")
        ]
        elapsed = sorted(int(item.get("elapsed_ms") or 0) for item in requests)
        if not elapsed:
            continue
        p50 = elapsed[len(elapsed) // 2]
        p90 = elapsed[min(len(elapsed) - 1, len(elapsed) * 9 // 10)]
        ok = sum(1 for item in requests if item.get("ok") is True)
        timings.append(
            f"{name}={ok}/{len(elapsed)}:{_seconds(p50)}/{_seconds(p90)}/{_seconds(elapsed[-1])}"
        )
        direct = sum(1 for item in requests if item.get("direct_fallback") is True)
        if direct:
            fallbacks.append(f"{name}={direct}")
        reused = sum(1 for item in requests if item.get("shared") is True)
        if reused:
            shared.append(f"{name}={reused}")
    exit_ips = [record.get("exit_ip") for record in records if record.get("exit_ip")]
    parts = [f"sources[{' '.join(timings)}]"]
    if fallbacks:
        parts.append(f"direct_fallback[{' '.join(fallbacks)}]")
    if shared:
        parts.append(f"shared[{' '.join(shared)}]")
    parts.append(f"exit_ips={len(exit_ips)} unique_exit_ips={len(set(exit_ips))}")
    return parts


def _timing_summary(
    download_ms: int,
    job: Any,
    nodes: list[dict[str, Any]] | None,
    records: list[dict[str, Any]] | None = None,
) -> str:
    """一行聚合耗时，给 Actions 日志找瓶颈用：只有数字，没有节点名、出口 IP 或订阅内容。

    节点用时取逐节点实时快照的 ms（含被丢弃的失败尝试），phase_sum 是各节点各相之和
    （并行下会远大于墙钟，看的是比例）。sources 是各上游「成功数/请求数:p50/p90/max」。
    """

    metrics = job.get("metrics") if isinstance(job, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    parts = [f"subscription={_seconds(download_ms)}"]
    if isinstance(metrics.get("wall_ms"), int):
        parts.append(f"scan_wall={_seconds(metrics['wall_ms'])}")
    if nodes:
        elapsed = sorted(int(node.get("ms") or 0) for node in nodes)
        p50 = elapsed[len(elapsed) // 2]
        p90 = elapsed[min(len(elapsed) - 1, len(elapsed) * 9 // 10)]
        parts += [
            f"nodes={len(nodes)}",
            f"peak_parallel={int(metrics.get('peak_active_nodes') or 0)}",
            f"node_p50={_seconds(p50)}",
            f"node_p90={_seconds(p90)}",
            f"node_max={_seconds(elapsed[-1])}",
            f"retried={sum(1 for node in nodes if int(node.get('a') or 0) > 1)}",
            f"failed={sum(1 for node in nodes if node.get('s') == 'failed')}",
        ]
    phase_ms = metrics.get("phase_ms")
    if isinstance(phase_ms, dict):
        sums = " ".join(f"{name}={_seconds(int(value))}" for name, value in phase_ms.items())
        parts.append(f"phase_sum[{sums}]")
    parts += _source_timing(records or [])
    return "scan_timing: " + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        failure = ScanCLIError("scan_cancelled")
    except ScanCLIError as exc:
        failure = exc
    except Exception:
        failure = ScanCLIError("environment_not_ready")
    else:
        print("scan_completed: 已确认清理并发布脱敏扫描产物")
        return 0
    print(f"{failure.code}: {failure}", file=sys.stderr)
    return failure.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
