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
async def test_chapter_versions_can_be_selected_by_owner(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="version-owner", slug="version-book", title="Version book")
        await SqlAlchemyProjectRepository(session).add(project)
        chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
        repository = SqlAlchemyChapterRepository(session)
        await repository.add(chapter)
        first = await repository.append_version(chapter_id=chapter.id, content="first")
        second = await repository.append_version(chapter_id=chapter.id, content="second")
        await session.commit()

        assert (await repository.get_version_owned(chapter.id, first.id, project.owner_id)).content == "first"
        assert await repository.get_version_owned(chapter.id, second.id, "other-owner") is None
