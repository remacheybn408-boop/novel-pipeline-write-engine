"""SQLite 上 UnitOfWork 主要 repository 的 smoke（V15-003）。

在真实 SQLite 文件（经 alembic upgrade head 建表）上验证 project /
chapter 的建查路径，包括 append_version 的方言感知锁分支。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.bootstrap import ensure_schema
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'proseforge.sqlite3').as_posix()}",
    )
    ensure_schema(settings)
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_create_get_and_list(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.projects.add(Project.create(owner_id="u1", slug="demo", title="演示"))
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        loaded = await uow.projects.get_by_slug("u1", "demo")
        assert loaded is not None
        assert loaded.title == "演示"
        assert loaded.status == "ACTIVE"
        assert loaded.language == "zh-CN"
        owned = await uow.projects.list_for_owner("u1")
        assert [project.slug for project in owned] == ["demo"]


@pytest.mark.asyncio
async def test_chapter_create_list_and_append_versions(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        project = await uow.projects.add(Project.create(owner_id="u1", slug="demo", title="演示"))
        chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=1, title="第一章"))
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        chapters = await uow.chapters.list_owned(project.id, "u1")
        assert [item.title for item in chapters] == ["第一章"]
        owned = await uow.chapters.get_owned(chapter.id, "u1")
        assert owned is not None
        assert owned.status == "PLANNED"

        first = await uow.chapters.append_version(chapter_id=chapter.id, content="正文一")
        assert first.version_no == 1
        second = await uow.chapters.append_version(chapter_id=chapter.id, content="正文二")
        assert second.version_no == 2
        # 相同内容按 content_hash 去重，不新增版本。
        duplicate = await uow.chapters.append_version(chapter_id=chapter.id, content="正文二")
        assert duplicate.id == second.id
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        versions = await uow.chapters.list_versions(chapter.id, "u1")
        assert [version.version_no for version in versions] == [2, 1]
        await uow.chapters.set_active_version(chapter.id, second.id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        contents = await uow.chapters.active_contents(project.id, "u1")
        assert [(item.chapter_no, content) for item, content in contents] == [(1, "正文二")]


@pytest.mark.asyncio
async def test_uow_rolls_back_uncommitted_changes(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.projects.add(Project.create(owner_id="u1", slug="discarded", title="丢弃"))
    # 未 commit 即退出：应 rollback。

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.projects.get_by_slug("u1", "discarded") is None
