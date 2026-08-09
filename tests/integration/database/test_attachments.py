import pytest

from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.repositories.attachment import (
    SqlAlchemyAttachmentRepository,
)


@pytest.mark.asyncio
async def test_attachment_listing_is_owner_scoped(session_factory):
    async with session_factory() as session:
        session.add(ProjectModel(id="p-files", owner_id="u1", slug="files", title="Files"))
        await session.flush()
        repository = SqlAlchemyAttachmentRepository(session)
        await repository.add("p-files", "outline.md", "a" * 64, "sha256/aa/bb/key")
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyAttachmentRepository(session)
        assert len(await repository.list_owned("p-files", "u1")) == 1
        assert await repository.list_owned("p-files", "u2") == []
