"""require_work_project mode assertion (sqlite in-memory via aiosqlite).

Work-only endpoints call this helper; chat-mode, missing, and foreign
projects must all surface as the same 404 (no existence leak across modes).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.api.dependencies import require_work_project
from proseforge.infrastructure.database import (
    models,  # noqa: F401  # register metadata
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine


@pytest_asyncio.fixture
async def session_factory():
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ProjectModel(id="pw", owner_id="u1", slug="pw", title="W", mode="work"))
        session.add(ProjectModel(id="pc", owner_id="u1", slug="pc", title="C", mode="chat"))
        session.add(ProjectModel(id="po", owner_id="u2", slug="po", title="O", mode="work"))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_work_project_passes(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        project = await require_work_project(uow, "u1", "pw")
    assert project.id == "pw" and project.mode == "work"


@pytest.mark.asyncio
async def test_chat_project_is_404(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        with pytest.raises(HTTPException) as excinfo:
            await require_work_project(uow, "u1", "pc")
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "project not found"  # same as missing: no mode leak


@pytest.mark.asyncio
async def test_missing_and_foreign_projects_are_404(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        with pytest.raises(HTTPException) as missing:
            await require_work_project(uow, "u1", "nope")
        with pytest.raises(HTTPException) as foreign:
            await require_work_project(uow, "u1", "po")  # owned by u2
    assert missing.value.status_code == 404 and foreign.value.status_code == 404
