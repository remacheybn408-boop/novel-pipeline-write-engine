import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.api.sse.encoder import encode_sse
from proseforge.infrastructure.database import (
    models,  # noqa: F401  # register metadata
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.sqlite import create_sqlite_engine
from proseforge.infrastructure.events.database import DatabaseEventStream
from proseforge.infrastructure.events.memory import InMemoryEventStream


def test_sse_encoder_keeps_event_id():
    assert encode_sse(event_id="42", event="content.delta", data={"text": "hi"}).startswith(b"id: 42\n")


@pytest.mark.asyncio
async def test_inmemory_subscribe_live_tail_receives_new_events_until_terminal():
    stream = InMemoryEventStream(poll_seconds=0.01)
    await stream.publish("conversation:c1", {"event": "content.delta", "text": "one"})
    received = []

    async def consume():
        async for event in stream.subscribe("conversation:c1"):
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # replay drained; subscriber keeps tailing
    await stream.publish("conversation:c1", {"event": "content.delta", "text": "two"})
    await asyncio.sleep(0.05)
    assert not task.done(), "subscribe must keep polling after the replay is drained"
    await stream.publish("conversation:c1", {"event": "message.completed", "message_id": "m1"})
    await asyncio.wait_for(task, timeout=2)
    assert [event.get("text") for event in received if event["event"] == "content.delta"] == ["one", "two"]
    assert received[-1]["event"] == "message.completed"


@pytest.mark.asyncio
async def test_inmemory_subscribe_stops_at_terminal_event_during_replay():
    stream = InMemoryEventStream(poll_seconds=0.01)
    await stream.publish("conversation:c2", {"event": "content.delta", "text": "one"})
    await stream.publish("conversation:c2", {"event": "message.failed", "message_id": "m1"})
    await stream.publish("conversation:c2", {"event": "content.delta", "text": "three"})

    events = [event async for event in stream.subscribe("conversation:c2", "1")]

    assert [event["event"] for event in events] == ["message.failed"]


@pytest.mark.asyncio
async def test_database_subscribe_live_tail_on_sqlite(tmp_path):
    engine = create_sqlite_engine(tmp_path / "events.db")
    try:
        async with engine.begin() as connection:
            # Full schema: conversation_events now FK-references conversations,
            # so the parent chain must exist (this engine enforces FKs).
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory() as session:
            session.add(ProjectModel(id="p1", owner_id="u1", slug="p1", title="t"))
            await session.flush()
            session.add(ConversationModel(id="c1", project_id="p1", title="t"))
            await session.commit()
        stream = DatabaseEventStream(factory, poll_seconds=0.01)
        await stream.publish("conversation:c1", {"event": "content.delta", "text": "one"})
        received = []

        async def consume():
            async for event in stream.subscribe("conversation:c1"):
                received.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        await stream.publish("conversation:c1", {"event": "content.delta", "text": "two"})
        await asyncio.sleep(0.05)
        assert not task.done(), "subscribe must keep polling after the replay is drained"
        await stream.publish("conversation:c1", {"event": "message.failed", "message_id": "m1"})
        await asyncio.wait_for(task, timeout=2)
        assert [event.get("text") for event in received if event["event"] == "content.delta"] == ["one", "two"]
        assert received[-1]["event"] == "message.failed"
        assert received[-1]["id"] == "3"
    finally:
        await engine.dispose()
