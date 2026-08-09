from __future__ import annotations

import hashlib
import logging
import time

from proseforge.application.conversations.terminal_state import terminal_message_status
from proseforge.domain.usage import UsageDelta
from proseforge.providers.errors import classify_provider_error
from proseforge.providers.usage import normalize_provider_usage

logger = logging.getLogger(__name__)


class _ChunkBatcher:
    """Batch content.delta chunk persistence: one DB transaction per
    flush_seconds or batch_size frames instead of one transaction per frame.

    Chunk indexes are assigned at buffering time (gaps are fine — the
    frontend sorts by index). flush() persists every pending frame in a
    single uow and returns True when the message turned out CANCELLED
    (nothing is written in that case). SSE publishes stay per-frame; only
    the DB writes are batched.
    """

    def __init__(self, uow_factory, message_id: str, *, flush_seconds: float = 0.2, batch_size: int = 40):
        self._uow_factory = uow_factory
        self._message_id = message_id
        self._flush_seconds = flush_seconds
        self._batch_size = batch_size
        self._pending: list[tuple[int, str]] = []
        self._last_flush = time.monotonic()
        self.flush_calls = 0
        self.flush_seconds_total = 0.0

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def add(self, index: int, text: str) -> bool:
        """Buffer one frame; flush when the batch is full or stale. True = CANCELLED."""
        self._pending.append((index, text))
        if len(self._pending) >= self._batch_size or time.monotonic() - self._last_flush >= self._flush_seconds:
            return await self.flush()
        return False

    async def flush(self) -> bool:
        """Write all pending frames in one transaction. True = CANCELLED (nothing written)."""
        if not self._pending:
            self._last_flush = time.monotonic()
            return False
        batch, self._pending = self._pending, []
        started = time.monotonic()
        cancelled = False
        async with self._uow_factory() as uow:
            status_reader = getattr(uow.conversations, "message_status", None)
            if status_reader and await status_reader(self._message_id) == "CANCELLED":
                cancelled = True  # drop the batch: a cancelled message takes no more content
            else:
                for index, text in batch:
                    await uow.conversations.append_chunk(self._message_id, index, "content.delta", text)
                await uow.commit()
        self.flush_calls += 1
        self.flush_seconds_total += time.monotonic() - started
        self._last_flush = time.monotonic()
        return cancelled


class _CancelProbe:
    """Throttled CANCELLED check for the streaming loop.

    A user stop flips the message to CANCELLED while the provider stream is
    still delivering frames; without a per-frame check the batcher only
    notices on the next flush window, and late frames keep the frontend
    bubble stuck in "generating". Checking the status on every frame would
    cost one DB read per frame, so the result is cached for ``interval``
    seconds.
    """

    def __init__(self, uow_factory, message_id: str, *, interval: float = 0.5):
        self._uow_factory = uow_factory
        self._message_id = message_id
        self._interval = interval
        self._last_check = 0.0
        self._cancelled = False

    async def cancelled(self) -> bool:
        if self._cancelled:
            return True
        now = time.monotonic()
        if now - self._last_check < self._interval:
            return False
        self._last_check = now
        async with self._uow_factory() as uow:
            status_reader = getattr(uow.conversations, "message_status", None)
            if status_reader is not None and await status_reader(self._message_id) == "CANCELLED":
                self._cancelled = True
        return self._cancelled


class GenerateReply:
    def __init__(self, uow_factory, provider, event_stream=None):
        self.uow_factory = uow_factory
        self.provider = provider
        self.event_stream = event_stream
        self.publish_seconds = 0.0

    async def _publish(self, conversation_id: str | None, message_id: str, payload: dict) -> None:
        if not self.event_stream:
            return
        started = time.monotonic()
        try:
            await self.event_stream.publish(f"message:{message_id}", payload)
            if conversation_id:
                await self.event_stream.publish(f"conversation:{conversation_id}", payload)
        finally:
            self.publish_seconds += time.monotonic() - started

    async def _rewrite_file_blocks(self, uow, message_id: str, content: str) -> str:
        """Persist ```file blocks as attachments and rewrite the message content.

        Returns the content to hash/publish. Falls back to the original text
        when there is nothing to do, the repo lacks a content writer, or any
        extraction step fails — extraction must never fail a generation.
        """
        if "```file:" not in content:
            return content
        try:
            from proseforge.application.conversations.file_blocks import (
                extract_and_rewrite,
            )

            rewritten = await extract_and_rewrite(uow, message_id=message_id, content=content)
            if rewritten != content:
                writer = getattr(uow.conversations, "set_message_content", None)
                if writer is None:
                    return content
                await writer(message_id, rewritten)
            return rewritten
        except Exception:
            return content

    async def execute(self, *, message_id: str, request, user_id: str = "", provider: str = "unknown", model: str = "unknown"):
        chunks = 0
        saw_usage = False
        finish_reason = ""
        call_id = f"message:{message_id}"
        started_at = time.monotonic()
        first_frame_at: float | None = None
        batcher = _ChunkBatcher(self.uow_factory, message_id)
        async with self.uow_factory() as uow:
            count = getattr(uow.conversations, "chunk_count", None)
            if count is not None:
                chunks = await count(message_id)
        conversation_id: str | None = None
        probe = _CancelProbe(self.uow_factory, message_id)
        try:
            if await probe.cancelled():
                # Cancelled before the stream opened: never flip CANCELLED back
                # to STREAMING and never publish message.started.
                return chunks
            async with self.uow_factory() as uow:
                await uow.conversations.set_message_status(message_id, "STREAMING")
                lookup = getattr(uow.conversations, "conversation_id_for_message", None)
                conversation_id = await lookup(message_id) if lookup else None
                await uow.commit()
            await self._publish(conversation_id, message_id, {"event": "message.started", "message_id": message_id})
            async for event in self.provider.stream(request):
                if first_frame_at is None:
                    first_frame_at = time.monotonic()
                    logger.info("generate_reply first frame message_id=%s first_frame_ms=%d", message_id, int((first_frame_at - started_at) * 1000))
                if event.event in {"response.failed", "error"}:
                    # provider 显式失败信号必须走错误路径：置 FAILED/PARTIAL 并广播
                    # message.failed（下方 except 统一处理），绝不静默翻 COMPLETED。
                    detail = event.data.get("error") or event.data.get("message") or event.data.get("response") or event.data
                    raise RuntimeError(f"provider stream failed: {detail}")
                if event.event == "response.completed":
                    # Capture finish_reason before the usage branch below may
                    # `continue` past it (completed events can carry usage).
                    finish_reason = str(event.data.get("finish_reason") or finish_reason)
                if event.event == "usage.updated":
                    saw_usage = True
                    async with self.uow_factory() as uow:
                        delta = normalize_provider_usage(provider, event.data, final=bool(event.data.get("final")))
                        usage_repo = getattr(uow, "usage", None)
                        if usage_repo:
                            await usage_repo.record(user_id=user_id, provider=provider, model_id=model, call_id=call_id, delta=delta, message_id=message_id, conversation_id=conversation_id)
                            await uow.commit()
                    if self.event_stream:
                        usage_payload = {"event": "usage.updated", "message_id": message_id, **delta.as_event_payload()}
                        await self._publish(conversation_id, message_id, usage_payload)
                    continue
                if event.event == "response.completed" and event.data.get("usage"):
                    saw_usage = True
                    async with self.uow_factory() as uow:
                        delta = normalize_provider_usage(provider, event.data, final=True)
                        usage_repo = getattr(uow, "usage", None)
                        if usage_repo:
                            await usage_repo.record(user_id=user_id, provider=provider, model_id=model, call_id=call_id, delta=delta, message_id=message_id, conversation_id=conversation_id)
                            await uow.commit()
                    if self.event_stream:
                        usage_payload = {"event": "usage.updated", "message_id": message_id, **delta.as_event_payload()}
                        await self._publish(conversation_id, message_id, usage_payload)
                    continue
                if event.event == "reasoning.delta":
                    # Reasoning stream: SSE passthrough only — no chunks, no
                    # body content, no hash impact.
                    await self._publish(conversation_id, message_id, {"event": "reasoning.delta", "message_id": message_id, "text": event.text})
                    continue
                if event.event != "content.delta":
                    continue
                if await probe.cancelled():
                    # Stop late frames reaching a cancelled message instead of
                    # waiting for the next batcher flush window.
                    return chunks
                # SSE stays per-frame; only the DB chunk writes are batched.
                index = chunks
                chunks += 1
                if self.event_stream:
                    payload = {"event": event.event, "message_id": message_id, "index": index, "text": event.text}
                    await self._publish(conversation_id, message_id, payload)
                if await batcher.add(index, event.text):
                    return chunks  # CANCELLED detected during a batched flush
            if await batcher.flush():
                return chunks  # CANCELLED while flushing the residual buffer
            content_hash = None
            missing_usage = None
            async with self.uow_factory() as uow:
                status_reader = getattr(uow.conversations, "message_status", None)
                if status_reader and await status_reader(message_id) == "CANCELLED":
                    return chunks
                message_reader = getattr(uow.conversations, "get_message", None)
                message = await message_reader(message_id) if message_reader else None
                final_content = message.content if message else ""
                if not final_content.strip():
                    # Ghost completion: provider ended the stream with zero body
                    # (e.g. reasoning burned the whole max_tokens budget). Never
                    # mark an empty message COMPLETED — fail it visibly instead.
                    reason = "max-tokens-exhausted" if finish_reason == "length" else "empty-completion"
                    await uow.conversations.set_message_status(message_id, "FAILED")
                    await uow.commit()
                    await self._publish(conversation_id, message_id, {"event": "message.failed", "message_id": message_id, "status": "FAILED", "reason": reason})
                    return chunks
                await uow.conversations.set_message_status(message_id, "COMPLETED")
                # Swap ```file blocks for download links before hashing: the hash
                # must cover the rewritten content the user actually sees.
                final_content = await self._rewrite_file_blocks(uow, message_id, final_content)
                content_hash = hashlib.sha256(final_content.encode()).hexdigest()
                hash_writer = getattr(uow.conversations, "set_content_hash", None)
                if hash_writer:
                    await hash_writer(message_id, content_hash)
                usage_repo = getattr(uow, "usage", None)
                if usage_repo and not saw_usage:
                    # provider 全程未回 usage → 显式记 source="missing"，不假装是 provider 值。
                    missing_usage = UsageDelta(source="missing", final=True)
                    await usage_repo.record(user_id=user_id, provider=provider, model_id=model, call_id=call_id, delta=missing_usage, message_id=message_id, conversation_id=conversation_id)
                await uow.commit()
            if missing_usage is not None:
                await self._publish(conversation_id, message_id, {"event": "usage.updated", "message_id": message_id, **missing_usage.as_event_payload()})
            await self._publish(conversation_id, message_id, {"event": "message.completed", "message_id": message_id, "status": "COMPLETED", "content_hash": content_hash})
            logger.info(
                "generate_reply completed message_id=%s frames=%d publish_ms=%d chunk_flush_ms=%d",
                message_id, chunks, int(self.publish_seconds * 1000), int(batcher.flush_seconds_total * 1000),
            )
        except Exception as error:
            try:
                # Persist residual buffered frames before the terminal state.
                await batcher.flush()
            except Exception:  # noqa: S110 -- best-effort flush: the original error is what matters here
                pass  # the original error is what matters here
            terminal_status = None
            async with self.uow_factory() as uow:
                status_reader = getattr(uow.conversations, "message_status", None)
                if not status_reader or await status_reader(message_id) != "CANCELLED":
                    terminal_status = terminal_message_status(chunks)
                    await uow.conversations.set_message_status(message_id, terminal_status)
                    await uow.commit()
            if terminal_status is not None:
                await self._publish(conversation_id, message_id, {"event": "message.failed", "message_id": message_id, "status": terminal_status})
            raise classify_provider_error(error) from error
        return chunks
