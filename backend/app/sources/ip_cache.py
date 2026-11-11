"""同一任务内按出口 IP 复用请求：订阅里常有多个入口落到同一个出口。

只用于答案与「从哪个节点问」无关的请求（注册表里 `shareable` 的那些：lookup、IPQS、
IPure、Coffee 的 ping / 端口 / 连通检测）。单飞：同一 (源, IP) 正在问时，后来者等它的结果。

只复用成功的答案。先问的节点失败（多半是它自己的代理不通），等着的节点各自重问，
不把别人的连接故障算到自己头上。先问的节点被取消（它那组作废或节点超时）时，
请求跟着取消，等着的节点接手重问。复用来的结果带 `shared: true`，`proxy_url`
保留真正发出请求的那个节点的代理地址，审计时看得出这份数据是谁代问的。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class IpCache:
    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self.hits = 0

    async def fetch(
        self,
        source: str,
        ip: str,
        request: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        key = (source, ip)
        while True:
            task = self._tasks.get(key)
            if task is None:
                return await self._own(key, request)
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                continue  # 先问的节点被取消：接手重问。
            if result.get("ok"):
                self.hits += 1
                return {**result, "shared": True}
            # 先问的节点失败：自己问一次（失败的任务已从表里摘掉）。
            return await request()

    async def _own(
        self, key: tuple[str, str], request: Callable[[], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        task = asyncio.ensure_future(request())
        self._tasks[key] = task
        try:
            result = await asyncio.shield(task)
        except BaseException:
            task.cancel()
            self._drop(key, task)
            raise
        if not result.get("ok"):
            self._drop(key, task)
        return result

    def _drop(self, key: tuple[str, str], task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
