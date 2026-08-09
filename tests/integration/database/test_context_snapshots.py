from __future__ import annotations

import pytest

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.context import (
    SqlAlchemyContextRepository,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_context_snapshot_is_durable_and_owner_scoped(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="snapshot-owner", slug="snapshot-book", title="Snapshot book")
        await SqlAlchemyProjectRepository(session).add(project)
        repository = SqlAlchemyContextRepository(session)
        item = await repository.add(project.id, "manual", "A durable story fact")
        snapshot = await repository.snapshot(project.id, [item])
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyContextRepository(session)
        found = await repository.get_snapshot_owned(snapshot.id, "snapshot-owner")
        assert found is not None
        assert await repository.get_snapshot_owned(snapshot.id, "other-owner") is None
