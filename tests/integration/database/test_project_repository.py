from __future__ import annotations

import pytest

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_project_repository_round_trip(session_factory):
    async with session_factory() as session:  # type: AsyncSession
        repository = SqlAlchemyProjectRepository(session)
        project = Project.create(owner_id="u1", slug="book", title="Book")
        await repository.add(project)
        await session.commit()

    async with session_factory() as session:
        found = await SqlAlchemyProjectRepository(session).get_by_slug("u1", "book")
        assert found is not None
        assert found.title == "Book"
