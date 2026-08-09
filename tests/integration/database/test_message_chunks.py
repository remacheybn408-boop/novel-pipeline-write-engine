import pytest

from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.conversation import (
    SqlAlchemyConversationRepository,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_duplicate_chunk_is_idempotent(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="chunk-owner", slug="chunk-book", title="Chunk book")
        await SqlAlchemyProjectRepository(session).add(project)
        repository = SqlAlchemyConversationRepository(session)
        branch = await repository.create(Conversation.create(project.id))
        message = await repository.append_message(branch.id, "assistant", "stream")
        first = await repository.append_chunk(message.id, 0, "content.delta", "hel")
        second = await repository.append_chunk(message.id, 0, "content.delta", "hel")
        assert second.id == first.id


@pytest.mark.asyncio
async def test_duplicate_client_request_reuses_existing_assistant(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="idempotent-owner", slug="idempotent-book", title="Idempotent book")
        await SqlAlchemyProjectRepository(session).add(project)
        repository = SqlAlchemyConversationRepository(session)
        branch = await repository.create(Conversation.create(project.id))
        user = await repository.append_message(branch.id, "user", "hello", "client-1")
        assistant = await repository.append_message(branch.id, "assistant", "", None, "PENDING")
        duplicate = await repository.get_by_client_request_id("client-1")
        reused = await repository.assistant_after(duplicate.id)
        assert duplicate is not None and duplicate.id == user.id
        assert reused is not None and reused.id == assistant.id
