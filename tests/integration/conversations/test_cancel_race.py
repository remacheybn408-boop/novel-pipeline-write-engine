import pytest

from proseforge.application.conversations.generate_reply import GenerateReply
from proseforge.domain.ports.model_provider import GenerationEvent


class RaceProvider:
    async def stream(self, request):
        yield GenerationEvent("content.delta", "first")
        yield GenerationEvent("content.delta", "second")


class RaceRepo:
    def __init__(self):
        self.status = "CANCELLED"
        self.chunks = []
        self.statuses = []

    async def chunk_count(self, message_id): return 0
    async def message_status(self, message_id): return self.status
    async def set_message_status(self, message_id, status): self.statuses.append(status)
    async def append_chunk(self, *args): self.chunks.append(args)


class RaceUow:
    def __init__(self, repo): self.conversations = repo
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass


@pytest.mark.asyncio
async def test_cancelled_before_stream_writes_no_late_chunk():
    repo = RaceRepo()

    await GenerateReply(lambda: RaceUow(repo), RaceProvider()).execute(message_id="race", request=object())

    assert repo.chunks == []
    assert "COMPLETED" not in repo.statuses
