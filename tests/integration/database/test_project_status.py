from __future__ import annotations

import pytest

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_project_can_be_archived_and_restored(session_factory):
    async with session_factory() as session:
        repository = SqlAlchemyProjectRepository(session)
        project = Project.create(owner_id="status-owner", slug="status-book", title="Status book")
        await repository.add(project)
        await session.commit()

        archived = await repository.set_status(project.owner_id, project.id, "ARCHIVED")
        assert archived.status == "ARCHIVED"
        restored = await repository.set_status(project.owner_id, project.id, "ACTIVE")
        assert restored.status == "ACTIVE"
        await session.commit()
