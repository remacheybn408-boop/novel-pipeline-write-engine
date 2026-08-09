from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


class RuntimeLifecycle:
    """Own startup/shutdown ordering for the selected runtime profile."""

    def __init__(
        self,
        *,
        bootstrap: Callable[[], Any],
        queue: Any,
        scheduler: Any,
        engine: Any,
    ) -> None:
        self._bootstrap = bootstrap
        self._queue = queue
        self._scheduler = scheduler
        self._engine = engine
        self._started = False
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        if self._started:
            return
        self._ready = False
        started_queue = False
        try:
            await self._call(self._bootstrap)
            await self._call(self._queue.start)
            started_queue = True
            await self._call(self._scheduler.start)
            self._started = True
            self._ready = True
        except Exception:
            if started_queue:
                await self._call(self._queue.stop)
            raise

    async def stop(self) -> None:
        if not self._started:
            return
        self._ready = False
        self._started = False
        await self._call(self._scheduler.stop)
        await self._call(self._queue.stop)
        await self._close_engine()

    async def _close_engine(self) -> None:
        close = getattr(self._engine, "stop", None) or getattr(self._engine, "dispose", None)
        if close is not None:
            await self._call(close)

    @staticmethod
    async def _call(function: Callable[[], Any]) -> Any:
        result = function()
        if inspect.isawaitable(result):
            return await result
        return result

