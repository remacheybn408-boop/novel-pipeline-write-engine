import asyncio

import pytest

from proseforge.application.conversations.generate_reply import GenerateReply
from proseforge.domain.ports.model_provider import GenerationEvent


class Provider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "one")
        yield GenerationEvent("content.delta", "two")
        raise RuntimeError("network interruption")


class Repo:
    def __init__(self):
        self.statuses = []
        self.chunks = []

    async def set_message_status(self, message_id, status):
        self.statuses.append(status)

    async def append_chunk(self, *args):
        self.chunks.append(args)
    async def chunk_count(self, message_id): return 2


class Uow:
    def __init__(self, repo): self.conversations = repo
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass


class UsageProvider:
    async def stream(self, request):
        yield GenerationEvent("usage.updated", data={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7})
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}})


class EventStream:
    def __init__(self): self.events = []
    async def publish(self, topic, payload): self.events.append((topic, payload))


class CancelledRepo(Repo):
    def __init__(self):
        super().__init__()
        self.current_status = "PENDING"

    async def set_message_status(self, message_id, status):
        self.statuses.append(status)
        if self.current_status != "CANCELLED":
            self.current_status = status

    async def append_chunk(self, *args):
        self.chunks.append(args)
        self.current_status = "CANCELLED"

    async def message_status(self, message_id):
        return self.current_status


class TwoChunkProvider:
    async def stream(self, request):
        # Outlast the chunk batcher's flush window (0.2s) so the first chunk
        # is persisted before the cancellation lands; the second chunk must
        # then be dropped at the final flush.
        await asyncio.sleep(0.3)
        yield GenerationEvent("content.delta", "one")
        yield GenerationEvent("content.delta", "two")


class FailingProvider:
    async def stream(self, request):
        raise RuntimeError("provider unavailable")
        yield GenerationEvent("content.delta", "never")


class EmptyRepo(Repo):
    async def chunk_count(self, message_id): return 0


@pytest.mark.asyncio
async def test_interrupted_stream_marks_partial_after_persisting_chunks():
    repo = Repo()
    with pytest.raises(RuntimeError):
        await GenerateReply(lambda: Uow(repo), Provider()).execute(message_id="m", request=object())
    assert repo.chunks == [("m", 2, "content.delta", "one"), ("m", 3, "content.delta", "two")]
    assert repo.statuses[-1] == "PARTIAL"


@pytest.mark.asyncio
async def test_usage_events_are_forwarded_without_being_dropped():
    repo, events = Repo(), EventStream()
    await GenerateReply(lambda: Uow(repo), UsageProvider(), events).execute(message_id="m", request=object())
    usage = [payload for topic, payload in events.events if payload.get("event") == "usage.updated"]
    assert usage[0] == {
        "event": "usage.updated",
        "message_id": "m",
        "input_tokens": 4,
        "output_tokens": 3,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 7,
        "source": "provider",
        "final": False,
    }
    assert usage[-1]["final"] is True


@pytest.mark.asyncio
async def test_cancellation_stops_chunk_writes_and_preserves_cancelled_state():
    repo = CancelledRepo()
    await GenerateReply(lambda: Uow(repo), TwoChunkProvider()).execute(message_id="m", request=object())
    assert [chunk[3] for chunk in repo.chunks] == ["one"]
    assert "COMPLETED" not in repo.statuses


@pytest.mark.asyncio
async def test_provider_failure_without_output_is_failed_not_partial():
    repo = EmptyRepo()
    with pytest.raises(RuntimeError):
        await GenerateReply(lambda: Uow(repo), FailingProvider()).execute(message_id="m", request=object())
    assert repo.statuses[-1] == "FAILED"
