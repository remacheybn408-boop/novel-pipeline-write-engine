from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from sqlalchemy import func, select, text

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.dialect import capabilities_for_engine
from proseforge.infrastructure.database.models.conversation import (
    ConversationEventModel,
)
from proseforge.infrastructure.events.terminal import TERMINAL_EVENTS


async def append_events(session_factory, stream_key: str, events: list[dict[str, object]]) -> list[tuple[int, str, str]]:
    """Append a batch of events with contiguous sequences in ONE transaction.

    Returns ``(sequence, event_type, payload_json)`` per event. Shares the
    advisory-lock serialization with single-event publishes so SSE ids stay
    unique and ordered under concurrent writers (PG advisory lock; SQLite
    serializes writers with its database-level write lock).
    """
    if not events:
        return []
    async with session_factory() as session:
        if capabilities_for_engine(session.bind).supports_advisory_locks:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:stream_key))"),
                {"stream_key": stream_key},
            )
        next_sequence = await session.scalar(
            select(func.coalesce(func.max(ConversationEventModel.event_sequence), 0)).where(
                ConversationEventModel.conversation_id == stream_key
            )
        )
        sequence = int(next_sequence or 0)
        rows: list[tuple[int, str, str]] = []
        for event in events:
            sequence += 1
            event_type = str(event.get("event", "message"))
            payload = json.dumps(event, ensure_ascii=False)
            session.add(
                ConversationEventModel(
                    id=new_id(),
                    conversation_id=stream_key,
                    event_sequence=sequence,
                    event_type=event_type,
                    payload=payload,
                )
            )
            rows.append((sequence, event_type, payload))
        await session.commit()
        return rows


class DatabaseEventStream:
    """Durable event log used by SSE reconnects.

    The topic suffix is stored as the stream key so message and conversation
    streams can share one append-only table without leaking transport details
    into the domain models.

    subscribe 语义（V2-002）：回放 after_id 之后的事件 → 轮询新增
    （poll_seconds）→ 直到 terminal 事件（message.completed/failed）或
    订阅方取消。路由层负责 15s 心跳注释帧与断连清理。
    """

    def __init__(self, session_factory, poll_seconds: float = 1.0):
        self.session_factory = session_factory
        self.poll_seconds = poll_seconds

    async def publish(self, topic: str, event: dict[str, object]) -> None:
        stream_key = topic.split(":", 1)[-1]
        async with self.session_factory() as session:
            # MAX(sequence) + 1 is not safe when two workers publish to the
            # same conversation concurrently. Serialize allocation per stream
            # inside the transaction so SSE ids remain unique and ordered.
            # PG 用事务级 advisory lock；SQLite 由数据库级写锁串行化写入者。
            if capabilities_for_engine(session.bind).supports_advisory_locks:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:stream_key))"),
                    {"stream_key": stream_key},
                )
            next_sequence = await session.scalar(
                select(func.coalesce(func.max(ConversationEventModel.event_sequence), 0)).where(
                    ConversationEventModel.conversation_id == stream_key
                )
            )
            sequence = int(next_sequence or 0) + 1
            session.add(
                ConversationEventModel(
                    id=new_id(),
                    conversation_id=stream_key,
                    event_sequence=sequence,
                    event_type=str(event.get("event", "message")),
                    payload=json.dumps(event, ensure_ascii=False),
                )
            )
            await session.commit()

    async def aflush(self) -> None:
        """Interface symmetry with HybridEventStream.

        Publishes here are already synchronous one-transaction writes, so
        there is nothing to drain.
        """

    async def _fetch_after(self, stream_key: str, last: int) -> list[tuple[int, str, str]]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(
                    ConversationEventModel.event_sequence,
                    ConversationEventModel.event_type,
                    ConversationEventModel.payload,
                )
                .where(
                    ConversationEventModel.conversation_id == stream_key,
                    ConversationEventModel.event_sequence > last,
                )
                .order_by(ConversationEventModel.event_sequence)
            )
            return [(int(sequence), str(event_type), str(payload)) for sequence, event_type, payload in rows]

    async def subscribe(self, topic: str, after_id: str | None = None) -> AsyncIterator[dict[str, object]]:
        stream_key = topic.split(":", 1)[-1]
        last = int(after_id or "0")
        while True:
            rows = await self._fetch_after(stream_key, last)
            for sequence, event_type, raw_payload in rows:
                last = sequence
                payload = json.loads(raw_payload)
                yield {"id": str(last), "event": event_type, **payload}
                if event_type in TERMINAL_EVENTS:
                    return
            if not rows:
                await asyncio.sleep(self.poll_seconds)
