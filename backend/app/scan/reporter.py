"""扫描进行中的进度回报：runner → Worker 的 `/api/scan-progress`。

GitHub 在 job 结束前读不到日志，页面在扫描期间只能看到步骤名；这里大约每秒把「每个节点此刻
在做什么」（等待 / 启动代理 / 连接检测站 / 查询 IP 质量 / 重试 / 完整 / 部分 / 失败，外加已用时
与一句原因）和总计数报给 Worker，页面轮询时取回、逐个节点画出来。

三条硬约束：
- 只报过程，不报结果。每个节点只有名字、协议类型、阶段、第几次尝试、用时和一句**白名单**原因
  （`errors.live_reason`）；出口 IP、评分、订阅内容一律不出 runner——它们只在终态 artifact 里，
  而 artifact 要经过脱敏与逐字节校验。节点名里只要含有订阅地址或凭证里的值（与产物同一套
  `contains_forbidden_value` 判据），就换成「节点 N」；
- 尽力而为。任何失败（网络、4xx/5xx、超时）只记一笔，绝不抛出、绝不拖住扫描；
- 报告的身份是 request_id + run_id + run_attempt，Worker 只把与当前运行对得上的那条交给页面。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..results.artifact import contains_forbidden_value

PROGRESS_PATH = "/api/scan-progress"
# 每个节点的阶段是秒级变化的（启动代理约 0.5 秒、连检测站 1~5 秒），2 秒一跳会整段跳过；
# 有变化才发，所以节点都在等网络时不会白发。
_REPORT_INTERVAL_SECONDS = 0.75
# 没有阶段变化时也隔一阵报一次：用时在页面上本地走，心跳只是把它校准一下。
_HEARTBEAT_SECONDS = 5.0
_REQUEST_TIMEOUT_SECONDS = 4.0
_PHASES = ("subscription", "scanning", "packaging", "done")
# 与 worker/progress.js 同一份上限：超过就只报计数（几百个节点的订阅照样有进度条）。
MAX_REPORT_NODES = 1000
_NAME_CHARS = 64
_LIVE_STATES = {"wait", "retry", "start", "connect", "lookup", "success", "partial", "failed"}
_TYPE_RE = re.compile(r"[a-z0-9-]{1,24}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def progress_url_from_relay(relay_url: str) -> str:
    """进度端点与订阅中继同属一个 Worker：取中继地址的 origin，换上进度路径。"""

    parts = urlsplit(relay_url)
    return urlunsplit((parts.scheme, parts.netloc, PROGRESS_PATH, "", ""))


def job_counts(job: dict[str, Any] | None) -> dict[str, int]:
    """从扫描任务的内存状态取一份自洽的计数（完成数 = 三种状态之和，且不超过总数）。"""

    job = job or {}
    success = int(job.get("success_count") or 0)
    partial = int(job.get("partial_count") or 0)
    failed = int(job.get("failed_count") or 0)
    completed = success + partial + failed
    total = max(int(job.get("total") or 0), completed)
    return {
        "total": total,
        "completed": completed,
        "success": success,
        "partial": partial,
        "failed": failed,
    }


def display_name(name: object, index: int, redact: set[str]) -> str:
    """节点名 → 页面上显示的那一份：去控制字符、压空白、截到 64 字；含有订阅里的敏感值就换掉。"""

    text = " ".join(_CONTROL_RE.sub(" ", str(name or "")).split())[:_NAME_CHARS]
    if not text or contains_forbidden_value(text, redact):
        return f"节点 {index + 1}"
    return text


def live_nodes(nodes: list[dict[str, Any]] | None, redact: set[str]) -> list[dict[str, Any]] | None:
    """扫描任务的节点快照 → 线上格式（短键：n 名称、t 类型、s 阶段、a 尝试、ms 用时、r 原因）。"""

    if not nodes or len(nodes) > MAX_REPORT_NODES:
        return None
    wire = []
    for index, node in enumerate(nodes):
        state = node.get("state")
        node_type = str(node.get("type") or "").lower()
        reason = node.get("reason")
        wire.append(
            {
                "n": display_name(node.get("name"), index, redact),
                "t": node_type if _TYPE_RE.fullmatch(node_type) else "",
                "s": state if state in _LIVE_STATES else "wait",
                "a": max(0, min(10, int(node.get("attempt") or 0))),
                "ms": max(0, min(3_600_000, int(node.get("elapsed_ms") or 0))),
                "r": reason[:60] if isinstance(reason, str) and reason else None,
            }
        )
    return wire


def phase_of(job: dict[str, Any] | None) -> str:
    status = (job or {}).get("status")
    if status == "running":
        return "scanning"
    if status == "completed":
        return "packaging"
    return "subscription"


class ProgressReporter:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        request_id: str,
        run_id: int,
        run_attempt: int,
        client_factory: Callable[..., Any] = httpx.AsyncClient,
    ) -> None:
        self.url = url
        self.token = token
        self.identity = {
            "request_id": request_id,
            "run_id": run_id,
            "run_attempt": run_attempt,
        }
        self.client_factory = client_factory
        self.sent = 0
        self.failed = 0
        self._last: tuple[Any, ...] | None = None
        self._client: Any = None

    async def __aenter__(self) -> ProgressReporter:
        self._client = self.client_factory(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def report(
        self,
        phase: str,
        counts: dict[str, int] | None = None,
        nodes: list[dict[str, Any]] | None = None,
    ) -> bool:
        if phase not in _PHASES or self._client is None:
            return False
        counts = counts or job_counts(None)
        payload = {**self.identity, "phase": phase, **counts}
        # 节点列表必须与计数对得上（Worker 会核对）：数量等于总数，才一起发。
        if nodes is not None and len(nodes) == counts["total"]:
            payload["nodes"] = nodes
        try:
            response = await self._client.post(
                self.url,
                json=payload,
                headers={"X-Best-IP-Relay-Token": self.token},
            )
            ok = response.status_code == 204
        except Exception:
            ok = False
        if ok:
            self.sent += 1
        else:
            self.failed += 1
        return ok

    async def follow(
        self,
        read_job: Callable[[], dict[str, Any] | None],
        read_nodes: Callable[[], list[dict[str, Any]] | None] | None = None,
        redact: set[str] | None = None,
    ) -> None:
        """跟着扫描任务的内存状态报，直到被取消；只在某个节点换了阶段、或心跳到期时发送。"""

        loop = asyncio.get_running_loop()
        last_sent_at = 0.0
        redact = redact or set()
        while True:
            job = read_job()
            phase = phase_of(job)
            counts = job_counts(job)
            nodes = live_nodes(read_nodes() if read_nodes else None, redact)
            # 「有没有变化」不看用时（它每一刻都在变）：只看阶段、尝试与原因。
            shape = tuple((node["s"], node["a"], node["r"]) for node in nodes or [])
            snapshot = (phase, *counts.values(), shape)
            now = loop.time()
            if snapshot != self._last or now - last_sent_at >= _HEARTBEAT_SECONDS:
                if await self.report(phase, counts, nodes):
                    self._last = snapshot
                last_sent_at = loop.time()
            await asyncio.sleep(_REPORT_INTERVAL_SECONDS)
