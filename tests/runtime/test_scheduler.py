from __future__ import annotations

import asyncio

import pytest

from proseforge.infrastructure.scheduler.local import LocalScheduler


@pytest.mark.asyncio
async def test_local_scheduler_runs_maintenance_and_stops_promptly() -> None:
    calls: list[str] = []

    async def tick() -> None:
        calls.append("tick")

    scheduler = LocalScheduler(tick, interval_seconds=0.01)
    await scheduler.start()
    await asyncio.sleep(0.03)
    await scheduler.stop()
    count_after_stop = len(calls)
    await asyncio.sleep(0.03)

    assert calls
    assert len(calls) == count_after_stop
