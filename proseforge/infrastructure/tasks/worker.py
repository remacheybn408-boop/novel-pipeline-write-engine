"""LocalTaskQueue 的 asyncio worker 池（V15-004）。

start：启动 concurrency 个 worker task；stop 为 graceful——先停新任务
（stop event + poll 等待立即唤醒），等 grace 期让在跑的 handler 完成，
超时则 cancel 并释放其 lease（job 回 PENDING，可被后续进程重新 claim）。
worker 只负责 claim/派发/收尾；claim、recover、release 的 SQL 语义都在
local.py（LocalTaskQueue）中。worker 0 每轮顺带跑一次 recover_expired。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proseforge.infrastructure.tasks.local import LocalTaskQueue

TaskHandler = Callable[[dict[str, object]], Awaitable[object]]


class LocalQueueWorker:
    def __init__(
        self,
        queue: LocalTaskQueue,
        handlers: dict[str, TaskHandler],
        *,
        concurrency: int = 2,
        poll_seconds: float = 1.0,
        grace_seconds: float = 5.0,
    ):
        self._queue = queue
        self._handlers = handlers
        self._concurrency = max(1, int(concurrency))
        self._poll_seconds = max(0.01, float(poll_seconds))
        self._grace_seconds = max(0.0, float(grace_seconds))
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._active_jobs: dict[str, int] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._tasks = [
            asyncio.create_task(self._run_loop(index), name=f"local-queue-worker-{index}")
            for index in range(self._concurrency)
        ]

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        # 停新任务：set 后所有在 poll 等待中的 worker 立即醒来并退出循环。
        self._stop_event.set()
        # grace 期：等在跑的 handler 完成。
        done, pending = await asyncio.wait(self._tasks, timeout=self._grace_seconds)
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks = []
        # 释放 lease：被 cancel 的 handler 已自行 release；这里兜底仍在
        # active 集合里的 job（例如 release 本身被打断）。
        for job_id, lease_token in list(self._active_jobs.items()):
            await self._queue.release(job_id, lease_token)
            self._active_jobs.pop(job_id, None)

    async def _run_loop(self, index: int) -> None:
        while not self._stop_event.is_set():
            if index == 0:
                await self._queue.recover_expired()
            job = await self._queue.claim()
            if job is None:
                await self._wait_for_next_poll()
                continue
            handler = self._handlers.get(job.task_name)
            self._active_jobs[job.id] = job.lease_token
            try:
                if handler is None:
                    raise KeyError(f"no handler registered for task {job.task_name!r}")
                await handler(job.payload)
            except asyncio.CancelledError:
                # graceful stop 超时被打断：释放 lease 让 job 可被重新 claim。
                await self._queue.release(job.id, job.lease_token)
                raise
            except Exception as exc:
                await self._queue.retry(job.id, job.lease_token, f"{type(exc).__name__}: {exc}")
            else:
                await self._queue.complete(job.id, job.lease_token)
            finally:
                self._active_jobs.pop(job.id, None)

    async def _wait_for_next_poll(self) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
        except TimeoutError:
            pass
