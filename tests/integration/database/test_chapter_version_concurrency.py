from __future__ import annotations

import asyncio

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
async def test_concurrent_chapter_versions_get_distinct_numbers(session_factory):
    async with session_factory() as session:
        project = Project.create(owner_id="version-lock-owner", slug="version-lock", title="Version lock")
        await SqlAlchemyProjectRepository(session).add(project)
        chapter = Chapter.create(project_id=project.id, chapter_no=1, title="Opening")
        await SqlAlchemyChapterRepository(session).add(chapter)
        await session.commit()

    async def append(content: str):
        async with session_factory() as session:
            version = await SqlAlchemyChapterRepository(session).append_version(chapter_id=chapter.id, content=content)
            await session.commit()
            return version.version_no

    assert sorted(await asyncio.gather(append("one"), append("two"))) == [1, 2]
