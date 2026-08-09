from __future__ import annotations

import asyncio

import pytest

from proseforge.runtime.lifecycle import RuntimeLifecycle


class FakeComponent:
    def __init__(self, calls: list[str], name: str) -> None:
        self.calls = calls
        self.name = name
        self.started = False

    async def start(self) -> None:
        self.calls.append(f"{self.name}.start")
        self.started = True

    async def stop(self) -> None:
        self.calls.append(f"{self.name}.stop")
        self.started = False


@pytest.mark.asyncio
async def test_lifecycle_bootstraps_before_ready_and_stops_in_reverse_order() -> None:
    calls: list[str] = []
    queue = FakeComponent(calls, "queue")
    scheduler = FakeComponent(calls, "scheduler")
    engine = FakeComponent(calls, "engine")
    lifecycle = RuntimeLifecycle(
        bootstrap=lambda: calls.append("bootstrap"),
        queue=queue,
        scheduler=scheduler,
        engine=engine,
    )

    assert lifecycle.ready is False
    await lifecycle.start()

    assert lifecycle.ready is True
    assert calls == ["bootstrap", "queue.start", "scheduler.start"]

    await lifecycle.stop()

    assert lifecycle.ready is False
    assert calls == [
        "bootstrap",
        "queue.start",
        "scheduler.start",
        "scheduler.stop",
        "queue.stop",
        "engine.stop",
    ]


@pytest.mark.asyncio
async def test_lifecycle_start_is_idempotent_and_failure_keeps_not_ready() -> None:
    calls: list[str] = []

    def fail_bootstrap() -> None:
        calls.append("bootstrap")
        raise RuntimeError("migration failed")

    lifecycle = RuntimeLifecycle(
        bootstrap=fail_bootstrap,
        queue=FakeComponent(calls, "queue"),
        scheduler=FakeComponent(calls, "scheduler"),
        engine=FakeComponent(calls, "engine"),
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        await lifecycle.start()

    assert lifecycle.ready is False
    assert calls == ["bootstrap"]


@pytest.mark.asyncio
async def test_lifecycle_stop_is_safe_before_start_and_after_second_stop() -> None:
    calls: list[str] = []
    lifecycle = RuntimeLifecycle(
        bootstrap=lambda: calls.append("bootstrap"),
        queue=FakeComponent(calls, "queue"),
        scheduler=FakeComponent(calls, "scheduler"),
        engine=FakeComponent(calls, "engine"),
    )

    await lifecycle.stop()
    await lifecycle.start()
    await lifecycle.stop()
    await lifecycle.stop()

    assert calls == [
        "bootstrap",
        "queue.start",
        "scheduler.start",
        "scheduler.stop",
        "queue.stop",
        "engine.stop",
    ]


@pytest.mark.asyncio
async def test_lifecycle_does_not_block_event_loop_while_stopping() -> None:
    calls: list[str] = []
    lifecycle = RuntimeLifecycle(
        bootstrap=lambda: calls.append("bootstrap"),
        queue=FakeComponent(calls, "queue"),
        scheduler=FakeComponent(calls, "scheduler"),
        engine=FakeComponent(calls, "engine"),
    )

    await lifecycle.start()
    marker = False

    async def mark() -> None:
        nonlocal marker
        await asyncio.sleep(0)
        marker = True

    await asyncio.gather(lifecycle.stop(), mark())

    assert marker is True
