from __future__ import annotations

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
async def test_branch_fork_point_must_belong_to_same_conversation(session_factory):
    async with session_factory() as session:
        projects = SqlAlchemyProjectRepository(session)
        await projects.add(Project.create(owner_id="branch-owner", slug="branch-a", title="A"))
        project_b = Project.create(owner_id="branch-owner", slug="branch-b", title="B")
        await projects.add(project_b)
        conversations = SqlAlchemyConversationRepository(session)
        project_a = await projects.get_by_slug("branch-owner", "branch-a")
        first = Conversation.create(project_a.id, "Conversation A")
        await conversations.create(first)
        second = Conversation.create(project_b.id, "Conversation B")
        branch_b = await conversations.create(second)
        source = await conversations.append_message(branch_b.id, "user", "private message")
        await session.commit()

        assert await conversations.fork_owned(first.id, source.id, "invalid", "branch-owner") is None
