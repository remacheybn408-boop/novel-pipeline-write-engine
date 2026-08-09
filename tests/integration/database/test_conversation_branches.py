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
async def test_branch_inherits_only_to_fork_point(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="fork-owner", slug="fork-book", title="Fork book")
        await SqlAlchemyProjectRepository(session).add(project)
        repository = SqlAlchemyConversationRepository(session)
        root = Conversation.create(project.id, "Draft")
        main = await repository.create(root)
        first = await repository.append_message(main.id, "user", "one")
        second = await repository.append_message(main.id, "assistant", "two")
        await repository.append_message(main.id, "user", "three")
        branch = await repository.fork(root.id, second.id, "Alternative")
        visible = await repository.list_visible_messages(branch.id)
        assert [item.id for item in visible] == [first.id, second.id]
