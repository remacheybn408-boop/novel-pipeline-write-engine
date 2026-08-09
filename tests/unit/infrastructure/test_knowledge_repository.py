"""Knowledge document repository: CRUD roundtrip + owner isolation."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.domain.knowledge.entity import KnowledgeDocument
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.knowledge import KnowledgeDocumentModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.repositories.knowledge import (
    SqlAlchemyKnowledgeRepository,
)
from tests.conftest import make_fk_engine

_TABLES = [ProjectModel.__table__, KnowledgeDocumentModel.__table__]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        session.add(ProjectModel(id="p2", owner_id="u2", slug="other", title="Other"))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_crud_roundtrip(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyKnowledgeRepository(session)
        document = KnowledgeDocument.create(project_id="p1", title="世界观", content="大陆编年史")
        created = await repo.create(document)
        await session.commit()
        assert created.id == document.id

        owned = await repo.get_owned(document.id, "p1", "u1")
        assert owned is not None
        assert owned.title == "世界观" and owned.content == "大陆编年史"
        assert owned.created_at is not None and owned.updated_at is not None

        listed = await repo.list_for_project("p1", "u1")
        assert [doc.id for doc in listed] == [document.id]

        updated = await repo.update(document.id, "p1", "u1", title="新标题", content="新内容")
        await session.commit()
        assert updated is not None
        assert updated.title == "新标题" and updated.content == "新内容"

        assert await repo.delete_owned(document.id, "p1", "u1") is True
        await session.commit()
        assert await repo.get_owned(document.id, "p1", "u1") is None
        assert await repo.list_for_project("p1", "u1") == []


@pytest.mark.asyncio
async def test_owner_isolation(session_factory):
    async with session_factory() as session:
        repo = SqlAlchemyKnowledgeRepository(session)
        document = KnowledgeDocument.create(project_id="p1", title="设定集")
        await repo.create(document)
        await session.commit()

        # Another user's id never matches u1's project.
        assert await repo.list_for_project("p1", "u2") == []
        assert await repo.get_owned(document.id, "p1", "u2") is None
        assert await repo.update(document.id, "p1", "u2", title="篡改") is None
        assert await repo.delete_owned(document.id, "p1", "u2") is False

        # Wrong project scope is invisible too.
        assert await repo.get_owned(document.id, "p2", "u2") is None
        assert await repo.delete_owned(document.id, "p2", "u2") is False

        # The real owner is unaffected by the foreign attempts above.
        still_there = await repo.get_owned(document.id, "p1", "u1")
        assert still_there is not None and still_there.title == "设定集"
