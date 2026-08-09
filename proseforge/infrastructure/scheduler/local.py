from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

SchedulerTick = Callable[[], Awaitable[None]]


class LocalScheduler:
    """Small durable-runtime maintenance loop.

    The scheduler only triggers maintenance callbacks. Work execution remains
    owned by the task queue, so a scheduler restart cannot duplicate jobs.
    """

    def __init__(self, tick: SchedulerTick, *, interval_seconds: float = 30.0) -> None:
        self._tick = tick
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="proseforge-local-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop_event.set()
        await task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                try:
                    await self._tick()
                except Exception:  # noqa: S112 -- retried on the next interval; one bad backend must not kill the scheduler
                    # Maintenance failures are retried on the next interval;
                    # one bad backend must not kill the scheduler forever.
                    continue
