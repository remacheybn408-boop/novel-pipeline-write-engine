"""Hybrid event stream: batched durable writes + Redis fan-out.

Same public interface as DatabaseEventStream (``publish`` / ``subscribe``).

publish() buffers events in an in-memory queue; a lazily started background
flush loop drains it every FLUSH_SECONDS (or immediately when a terminal or
non-content.delta event arrives), appends the batch in one advisory-locked
transaction (sequences stay contiguous, see database.append_events), then
PUBLISHes each ``{sequence, event_type, payload}`` envelope to the Redis
channel ``events:{stream_key}``.

subscribe() replays from the DB first (closing the gap before the live
tail), then tails the Redis channel, deduping sequences already seen; a DB
catch-up every CATCHUP_SECONDS covers Redis hiccups and the replay/subscribe
race window. Heartbeat frames stay the route layer's job.

Degradation: if Redis is unreachable at first use (lazy probe — ping), the
stream permanently behaves like DatabaseEventStream. The flush loop is a
daemon-style task started on first publish, so publish-only workers keep it
alive without any subscriber; process exit kills it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from proseforge.infrastructure.events.database import DatabaseEventStream, append_events
from proseforge.infrastructure.events.terminal import TERMINAL_EVENTS

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 0.05
CATCHUP_SECONDS = 5.0

# Constructor sentinel: an explicitly passed redis_client (incl. None) skips
# the lazy probe entirely — the test seam.
_PROBE = object()


class HybridEventStream:
    def __init__(self, session_factory, redis_url: str | None = None, redis_client=_PROBE):
        self._delegate = DatabaseEventStream(session_factory)
        self._session_factory = session_factory
        self._redis_url = redis_url
        if redis_client is _PROBE:
            self._redis = None
            self._probed = False
        else:
            self._redis = redis_client
            self._probed = True
        self._queues: dict[str, asyncio.Queue] = {}
        self._flush_task: asyncio.Task | None = None
        self._wakeup: asyncio.Event | None = None

    async def _client(self):
        """Resolve the Redis client lazily; None means degraded to DB polling."""
        if self._probed:
            return self._redis
        self._probed = True
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(self._redis_url or "redis://localhost:6379/0")
            await client.ping()
            self._redis = client
            logger.info("hybrid event stream: redis fan-out enabled")
        except Exception as exc:
            self._redis = None
            logger.warning("hybrid event stream: redis unavailable (%s), falling back to database polling", exc)
        return self._redis

    # --- publish path ---

    async def aflush(self) -> None:
        """Drain every in-memory queue NOW (batched DB write + redis fan-out).

        Must be awaited before the event loop closes: celery wraps each task
        in ``asyncio.run``, and terminal events still sitting in the queue
        (e.g. ``message.completed`` published at the very end of a task) are
        otherwise cancelled with the loop and lost. No-op when degraded to
        DatabaseEventStream (its publishes are already synchronous).
        """
        if await self._client() is None:
            return
        await self._flush_all()

    async def publish(self, topic: str, event: dict[str, object]) -> None:
        if await self._client() is None:
            await self._delegate.publish(topic, event)
            return
        stream_key = topic.split(":", 1)[-1]
        queue = self._queues.setdefault(stream_key, asyncio.Queue())
        await queue.put(event)
        self._ensure_flush_task()
        event_type = str(event.get("event", "message"))
        if event_type in TERMINAL_EVENTS or event_type != "content.delta":
            # Rare/terminal events must not wait for the batch window.
            self._wakeup.set()

    def _ensure_flush_task(self) -> None:
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=FLUSH_SECONDS)
            except TimeoutError:
                pass
            self._wakeup.clear()
            await self._flush_all()

    async def _flush_all(self) -> None:
        for stream_key, queue in list(self._queues.items()):
            events: list[dict[str, object]] = []
            while True:
                try:
                    events.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if events:
                await self._flush_stream(stream_key, events)

    async def _flush_stream(self, stream_key: str, events: list[dict[str, object]]) -> None:
        try:
            rows = await append_events(self._session_factory, stream_key, events)
        except Exception as exc:
            # Flush failure must never crash a generation; events are dropped
            # from the queue, so log loudly.
            logger.warning("hybrid event stream: flush of %d event(s) to %s failed: %s", len(events), stream_key, exc)
            return
        for sequence, event_type, payload in rows:
            try:
                envelope = json.dumps({"sequence": sequence, "event_type": event_type, "payload": payload}, ensure_ascii=False)
                await self._redis.publish(f"events:{stream_key}", envelope)
            except Exception as exc:
                # Subscribers recover via the periodic DB catch-up.
                logger.warning("hybrid event stream: redis publish to %s failed: %s", stream_key, exc)
                return

    # --- subscribe path ---

    async def subscribe(self, topic: str, after_id: str | None = None) -> AsyncIterator[dict[str, object]]:
        if await self._client() is None:
            async for event in self._delegate.subscribe(topic, after_id):
                yield event
            return
        stream_key = topic.split(":", 1)[-1]
        last = int(after_id or "0")
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(f"events:{stream_key}")
            # DB replay first: covers everything published before we joined.
            async for event, done in self._replay(stream_key, last):
                last = int(event["id"])
                yield event
                if done:
                    return
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=CATCHUP_SECONDS)
                if message is None:
                    # No Redis traffic for a while: DB catch-up (covers Redis
                    # hiccups and the replay/subscribe race window).
                    async for event, done in self._replay(stream_key, last):
                        last = int(event["id"])
                        yield event
                        if done:
                            return
                    continue
                try:
                    envelope = json.loads(message["data"])
                    sequence = int(envelope["sequence"])
                    event_type = str(envelope["event_type"])
                    payload = json.loads(envelope["payload"])
                except (KeyError, TypeError, ValueError):
                    continue
                if sequence <= last:
                    continue  # already replayed from the DB
                last = sequence
                yield {"id": str(last), "event": event_type, **payload}
                if event_type in TERMINAL_EVENTS:
                    return
        finally:
            close = getattr(pubsub, "aclose", None)
            if close is not None:
                await close()

    async def _replay(self, stream_key: str, last: int) -> AsyncIterator[tuple[dict[str, object], bool]]:
        for sequence, event_type, raw_payload in await self._delegate._fetch_after(stream_key, last):
            payload = json.loads(raw_payload)
            yield {"id": str(sequence), "event": event_type, **payload}, event_type in TERMINAL_EVENTS
