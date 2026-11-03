from __future__ import annotations

import asyncio
import ipaddress
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from ..async_utils import to_thread_uncancelled as _to_thread_uncancelled
from ..config import Settings
from ..mihomo import MihomoError, MihomoNotReadyError, MihomoProcess, MihomoStopError
from ..sources.collector import CoffeeCollector
from .errors import (
    ScanError,
    _attach_mihomo_error,
    _failed_node,
    _read_mihomo_error,
    classify_error,
)
from .models import NodeOutcome, NodeRecord

# 实时进度的回调：(阶段, 第几次尝试, 上一次失败的原因)。阶段只有 start / connect / lookup / retry，
# 只给 runner → Worker 的进度回报用，不进产物。
StageCallback = Callable[[str, int, str | None], None]


@dataclass(slots=True)
class _Attempt:
    number: int
    result: NodeRecord
    failure: ScanError | None
    mihomo: MihomoProcess | None
    dns_bootstrap_proxy: str | None

    @property
    def failed(self) -> bool:
        return self.result.get("status") == "failed"

    @property
    def retryable(self) -> bool:
        return self.failure is None or self.failure.retryable


class NodeRunner:
    def __init__(
        self,
        app_settings: Settings,
        *,
        mihomo_factory: Callable[..., Any] = MihomoProcess,
        collector_factory: Callable[..., Any] = CoffeeCollector,
    ) -> None:
        self.settings = app_settings
        self.mihomo_factory = mihomo_factory
        self.collector_factory = collector_factory

    async def run(
        self,
        *,
        job_id: str,
        work_dir: Path,
        proxies: list[dict[str, Any]],
        proxy: dict[str, Any],
        index: int,
        dns_bootstrap_candidates: list[str],
        outbound_interface: str | None,
        on_stage: StageCallback | None = None,
    ) -> NodeOutcome:
        """检测一个节点：先试一次；失败且值得重试时，余下的尝试**并行**跑，谁先成功用谁。

        为什么并行：永久连不上的节点每次尝试都要吃满 Mihomo 的 5 秒拨号超时，三次串行加退避约
        18 秒，整轮扫描的长尾就是这几个节点（见 docs/DECISIONS.md「扫描核心」）。
        第 2、3 次尝试本来就各用各的 DNS 引导出口、各起一个 Mihomo，彼此不依赖；并行之后
        这条长尾约 11 秒。判定口径不变：最多 max_node_attempts 次、取第一份非失败记录、
        全部失败时用编号最大的那次失败作终态。
        """

        phase_ms: dict[str, int] = {}
        proxy_by_name = {str(candidate.get("name") or ""): candidate for candidate in proxies}

        def attempt(number: int) -> Any:
            return self._attempt(
                job_id=job_id,
                work_dir=work_dir,
                proxy=proxy,
                proxy_by_name=proxy_by_name,
                index=index,
                number=number,
                dns_bootstrap_candidates=dns_bootstrap_candidates,
                outbound_interface=outbound_interface,
                phase_ms=phase_ms,
                on_stage=on_stage,
            )

        first = await attempt(1)
        failures = [first] if first.failed else []
        chosen = None if first.failed else first
        max_attempts = self.settings.max_node_attempts
        if chosen is None and first.retryable and max_attempts > 1:
            if on_stage:
                on_stage("retry", 2, _attempt_error(first.result))
            if self.settings.node_retry_backoff_ms:
                await asyncio.sleep(self.settings.node_retry_backoff_ms / 1000)
            chosen = await _first_success(
                [attempt(number) for number in range(2, max_attempts + 1)], failures
            )

        failures.sort(key=lambda item: item.number)
        final = chosen or failures[-1]
        result = final.result
        _attach_attempt_evidence(
            result,
            attempts_used=len(failures) + (1 if chosen else 0),
            max_attempts=max_attempts,
            attempt_errors=[_attempt_error(item.result) for item in failures],
            mihomo=final.mihomo,
            outbound_interface=outbound_interface,
            dns_bootstrap_proxy=final.dns_bootstrap_proxy,
        )
        failure = final.failure
        if result.get("status") == "success":
            failure = None
        elif failure is None:
            failure = ScanError(
                "node_transport" if result.get("status") == "failed" else "enrichment_unavailable",
                "collect",
                result.get("status") == "failed",
                "节点传输连接失败" if result.get("status") == "failed" else "节点采集不完整",
            )
        return NodeOutcome(result, phase_ms, failure)

    async def _attempt(
        self,
        *,
        job_id: str,
        work_dir: Path,
        proxy: dict[str, Any],
        proxy_by_name: dict[str, dict[str, Any]],
        index: int,
        number: int,
        dns_bootstrap_candidates: list[str],
        outbound_interface: str | None,
        phase_ms: dict[str, int],
        on_stage: StageCallback | None,
    ) -> _Attempt:
        """一次尝试：独立的 Mihomo 进程与工作目录，结束时（含被取消）确认清理。"""

        node_name = str(proxy["name"])
        node_type = str(proxy["type"])
        mihomo: MihomoProcess | None = None
        dns_bootstrap_proxy: str | None = None
        log_offset = 0
        failure_phase = "start"
        failure: ScanError | None = None
        result: NodeRecord
        try:
            dns_bootstrap_proxy = _dns_bootstrap_for_attempt(
                proxy,
                dns_bootstrap_candidates,
                proxy_by_name=proxy_by_name,
                node_index=index,
                attempt=number,
            )
            attempt_proxies = _attempt_proxies(
                proxy,
                proxy_by_name,
                dns_bootstrap_proxy=dns_bootstrap_proxy,
            )
            mihomo = self.mihomo_factory(
                self.settings.mihomo_path,
                work_dir / f"node-{index:04d}-attempt-{number:02d}",
                attempt_proxies,
                selector_names=[node_name],
                outbound_interface=outbound_interface,
                dns_bootstrap_proxy=dns_bootstrap_proxy,
            )
            if on_stage:
                on_stage("start", number, None)
            phase_started = perf_counter()
            try:
                await mihomo.start()
            finally:
                _record_phase(phase_ms, "start", phase_started)
            log_offset = mihomo.log_offset()
            failure_phase = "selector"
            phase_started = perf_counter()
            try:
                selected = await mihomo.select(node_name)
                if selected != node_name:
                    raise MihomoError("切换节点后未确认 selector 身份")
            finally:
                _record_phase(phase_ms, "select", phase_started)
            failure_phase = "collector"
            collector = self.collector_factory(
                mihomo.proxy_url,
                timeout_ms=self.settings.page_timeout_ms,
            )
            if on_stage:
                on_stage("connect", number, None)
            phase_started = perf_counter()
            try:
                result = await asyncio.wait_for(
                    collector.collect(
                        job_id=job_id,
                        node_index=index,
                        node_name=node_name,
                        node_type=node_type,
                        selected_proxy=selected,
                        mihomo_instance=mihomo.instance_id,
                        on_exit_ip=(
                            (lambda: on_stage("lookup", number, None)) if on_stage else None
                        ),
                    ),
                    timeout=self.settings.page_timeout_ms / 1000,
                )
            finally:
                _record_phase(phase_ms, "collect", phase_started)
            _attach_mihomo_error(result, mihomo, log_offset)
        except asyncio.CancelledError:
            raise
        except MihomoNotReadyError:
            raise
        except MihomoStopError:
            raise
        except Exception as exc:
            failure = classify_error(exc, phase=failure_phase)
            mihomo_error = _read_mihomo_error(mihomo, log_offset) if mihomo else ""
            result = _failed_node(
                job_id,
                index,
                node_name,
                node_type,
                exc,
                phase=failure_phase,
                mihomo_error=mihomo_error,
                mihomo=mihomo,
            )
        finally:
            if mihomo:
                phase_started = perf_counter()
                try:
                    await _cleanup_mihomo(mihomo, work_dir)
                finally:
                    _record_phase(phase_ms, "stop", phase_started)
        return _Attempt(number, result, failure, mihomo, dns_bootstrap_proxy)


async def _first_success(attempts: list[Any], failures: list[_Attempt]) -> _Attempt | None:
    """并行跑几次尝试，第一份非失败记录胜出，其余立刻取消；失败的那些追加进 failures。

    无论怎么退出（胜出、全部失败、某次尝试抛错、外层被取消），都要等每个尝试的 Mihomo
    清理落定再返回；任何一个清理未确认，就以 MihomoStopError 收口——与节点 worker 同口径。
    """

    tasks = [asyncio.create_task(item) for item in attempts]
    winner: _Attempt | None = None
    try:
        pending = set(tasks)
        while pending and winner is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=tasks.index):
                outcome = task.result()
                if outcome.failed:
                    failures.append(outcome)
                elif winner is None:
                    winner = outcome
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(item, MihomoStopError) for item in settled):
            raise MihomoStopError("Mihomo 子进程清理未确认")
    return winner


async def _cleanup_mihomo(mihomo: Any, work_dir: Path) -> None:
    async def cleanup() -> None:
        try:
            await mihomo.stop()
            if not await _to_thread_uncancelled(_remove_work_dir, mihomo.work_dir, work_dir):
                raise MihomoStopError("Mihomo 工作目录清理未确认")
        except asyncio.CancelledError:
            raise MihomoStopError("Mihomo 清理任务被取消") from None
        except MihomoStopError:
            raise
        except Exception as exc:
            raise MihomoStopError("Mihomo 进程或工作目录清理未确认") from exc

    operation = asyncio.create_task(cleanup())
    cancelled = False
    while True:
        try:
            await asyncio.shield(operation)
            break
        except asyncio.CancelledError:
            if operation.cancelled():
                raise MihomoStopError("Mihomo 清理任务被取消") from None
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _record_phase(phase_ms: dict[str, int], phase: str, started: float) -> None:
    phase_ms[phase] = phase_ms.get(phase, 0) + round((perf_counter() - started) * 1000)


def _dns_bootstrap_candidates(proxies: list[dict[str, Any]]) -> list[str]:
    """能代跑 DNS 的节点：server 是**全局 IPv4 字面量**。

    为什么必须限 IPv4：引导出口要替本机把 DoH 查询送到四个解析器
    （`mihomo.py` 的 `_DOH_RESOLVERS`——223.5.5.5 / 1.12.12.12 / 1.1.1.1 /
    8.8.8.8，全是 IPv4 字面量），所以出口本身必须能由本机的 IPv4 栈到达。
    IPv6 字面量节点只有在运行环境有 IPv6 出口时才可能连上；一旦被选中而连不上，
    这次尝试的 DNS 就全部失败，日志被读成「节点服务器域名无法解析」，
    于是一个 A 记录正常、本来能连的节点被判成失败并白烧三次尝试。
    见 docs/DECISIONS.md「扫描核心」。
    """
    candidates: list[str] = []
    for proxy in proxies:
        try:
            address = ipaddress.ip_address(str(proxy.get("server") or ""))
        except ValueError:
            continue
        name = str(proxy.get("name") or "")
        if address.version == 4 and address.is_global and name:
            candidates.append(name)
    return list(dict.fromkeys(candidates))


def _dns_bootstrap_for_attempt(
    proxy: dict[str, Any],
    candidates: list[str],
    *,
    proxy_by_name: dict[str, dict[str, Any]] | None = None,
    node_index: int,
    attempt: int,
) -> str | None:
    if not candidates:
        return None
    if _proxy_chain_requires_dns(proxy, proxy_by_name or {}):
        return candidates[(node_index + attempt - 1) % len(candidates)]
    return None


def _proxy_chain_requires_dns(
    proxy: dict[str, Any],
    proxy_by_name: dict[str, dict[str, Any]],
    visited: set[str] | None = None,
) -> bool:
    try:
        ipaddress.ip_address(str(proxy.get("server") or ""))
    except ValueError:
        return True

    dependency_name = proxy.get("dialer-proxy")
    if not isinstance(dependency_name, str) or not dependency_name:
        return False
    seen = set() if visited is None else visited
    if dependency_name in seen:
        return False
    dependency = proxy_by_name.get(dependency_name)
    if dependency is None:
        return False
    seen.add(dependency_name)
    return _proxy_chain_requires_dns(dependency, proxy_by_name, seen)


def _attempt_proxies(
    proxy: dict[str, Any],
    proxy_by_name: dict[str, dict[str, Any]],
    *,
    dns_bootstrap_proxy: str | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    included_names: set[str] = set()

    def include(candidate: dict[str, Any]) -> None:
        name = str(candidate.get("name") or "")
        if not name or name in included_names:
            return
        included_names.add(name)
        selected.append(candidate)
        dependency_name = candidate.get("dialer-proxy")
        if not isinstance(dependency_name, str) or not dependency_name:
            return
        dependency = proxy_by_name.get(dependency_name)
        if dependency is not None:
            include(dependency)

    include(proxy)
    if dns_bootstrap_proxy:
        bootstrap = proxy_by_name.get(dns_bootstrap_proxy)
        if bootstrap is None:
            raise MihomoError("DNS 引导节点不在当前订阅代理集合中")
        include(bootstrap)
    return selected


def _attempt_error(result: dict[str, Any]) -> str:
    error = str(result.get("transport_error") or result.get("error") or "节点检测失败")
    return " ".join(error.split())[:500]


def _attach_attempt_evidence(
    result: dict[str, Any],
    *,
    attempts_used: int,
    max_attempts: int,
    attempt_errors: list[str],
    mihomo: MihomoProcess | None,
    outbound_interface: str | None,
    dns_bootstrap_proxy: str | None,
) -> None:
    result["attempt_count"] = attempts_used
    result["retry_count"] = attempts_used - 1
    result["attempt_errors"] = list(attempt_errors)
    evidence = result.get("proxy_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        result["proxy_evidence"] = evidence
    evidence.update(
        {
            "outbound_interface": (mihomo.outbound_interface if mihomo else outbound_interface),
            "dns_bootstrap_proxy": (mihomo.dns_bootstrap_proxy if mihomo else dns_bootstrap_proxy),
            "fresh_mihomo_per_attempt": True,
            "max_attempts": max_attempts,
        }
    )


def _remove_work_dir(work_dir: Path, workspace: Path) -> bool:
    target = work_dir.resolve()
    root = workspace.resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("工作目录不在扫描工作区中")
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not target.exists()
