from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from proseforge.infrastructure.events.terminal import TERMINAL_EVENTS


class InMemoryEventStream:
    def __init__(self, poll_seconds: float = 1.0):
        self._events: dict[str, list[dict[str, object]]] = {}
        self._poll_seconds = poll_seconds

    async def publish(self, topic: str, event: dict[str, object]) -> None:
        events = self._events.setdefault(topic, [])
        events.append({"id": str(len(events) + 1), **event})

    async def subscribe(self, topic: str, after_id: str | None = None) -> AsyncIterator[dict[str, object]]:
        # 回放 after_id 之后的事件，然后轮询新增，直到 terminal 事件或订阅方取消。
        last = int(after_id or "0")
        while True:
            events = self._events.get(topic, [])
            fresh = [event for event in events if int(str(event["id"])) > last]
            for event in fresh:
                last = int(str(event["id"]))
                yield event
                if event.get("event") in TERMINAL_EVENTS:
                    return
            if not fresh:
                await asyncio.sleep(self._poll_seconds)
