from __future__ import annotations

import pytest

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.attachment import (
    SqlAlchemyAttachmentRepository,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_attachment_lookup_is_owner_scoped(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="file-owner", slug="file-book", title="File book")
        await SqlAlchemyProjectRepository(session).add(project)
        attachment = await SqlAlchemyAttachmentRepository(session).add(project.id, "outline.md", "a" * 64, "sha256/aa/bb/blob")
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyAttachmentRepository(session)
        assert (await repository.get_owned(attachment.id, "file-owner")).filename == "outline.md"
        assert await repository.get_owned(attachment.id, "other-owner") is None
