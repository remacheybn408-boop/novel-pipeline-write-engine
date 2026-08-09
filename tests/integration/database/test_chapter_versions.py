from __future__ import annotations

import pytest

from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.repositories.chapter import (
    SqlAlchemyChapterRepository,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)


@pytest.mark.asyncio
async def test_same_content_hash_does_not_create_second_version(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="hash-owner", slug="hash-book", title="Hash book")
        await SqlAlchemyProjectRepository(session).add(project)
        repository = SqlAlchemyChapterRepository(session)
        chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
        await repository.add(chapter)
        first = await repository.append_version(chapter_id=chapter.id, content="same")
        second = await repository.append_version(chapter_id=chapter.id, content="same")
        await session.commit()
        assert second.id == first.id
        assert second.version_no == first.version_no
