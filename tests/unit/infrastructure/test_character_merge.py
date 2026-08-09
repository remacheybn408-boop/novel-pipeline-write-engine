"""merge_extracted: user rows keep authority, auto rows absorb updates,
unmatched names insert source="auto" rows."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.repositories.character import (
    SqlAlchemyCharacterRepository,
)
from tests.conftest import make_fk_engine

_TABLES = [ProjectModel.__table__, CharacterModel.__table__]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        await session.commit()
    yield factory
    await engine.dispose()


def _seed_row(name: str, *, source: str, aliases: list[str] | None = None, summary: str = "", role: str = "", confidence: float = 1.0) -> CharacterModel:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return CharacterModel(
        id=f"char-{name}", project_id="p1", name=name,
        aliases_json=json.dumps(aliases or [], ensure_ascii=False),
        summary=summary, role=role, status="active", source=source, confidence=confidence,
        created_at=now, updated_at=now,
    )


async def _merge(factory, **kwargs) -> None:
    async with factory() as session:
        repo = SqlAlchemyCharacterRepository(session)
        await repo.merge_extracted("p1", **kwargs)
        await session.commit()


async def _row(factory, character_id: str) -> CharacterModel:
    async with factory() as session:
        return await session.get(CharacterModel, character_id)


@pytest.mark.asyncio
async def test_new_character_inserts_auto_row(session_factory):
    await _merge(session_factory, name="烛龙", aliases=["老龙"], summary="睁眼为昼", role="反派", chapter_no=3)
    row = await _row(session_factory, "char-烛龙")
    assert row is None  # id is generated, not name-derived
    async with session_factory() as session:
        from sqlalchemy import select

        rows = list((await session.scalars(select(CharacterModel))).all())
    assert len(rows) == 1
    assert rows[0].name == "烛龙"
    assert rows[0].source == "auto" and rows[0].confidence == 0.6
    assert rows[0].first_seen_chapter == 3 and rows[0].last_seen_chapter == 3
    assert json.loads(rows[0].aliases_json) == ["老龙"]


@pytest.mark.asyncio
async def test_user_row_only_updates_last_seen(session_factory):
    async with session_factory() as session:
        session.add(_seed_row("李雷", source="user", aliases=["雷子"], summary="手工简介", role="主角"))
        await session.commit()

    await _merge(session_factory, name="李雷", aliases=["小李"], summary="模型生成的简介", role="配角", chapter_no=7)

    row = await _row(session_factory, "char-李雷")
    assert row.summary == "手工简介"  # untouched
    assert row.role == "主角"  # untouched
    assert json.loads(row.aliases_json) == ["雷子"]  # untouched
    assert row.last_seen_chapter == 7  # only this moves
    assert row.source == "user" and row.confidence == 1.0


@pytest.mark.asyncio
async def test_auto_row_absorbs_alias_union_and_summary(session_factory):
    async with session_factory() as session:
        session.add(_seed_row("韩梅梅", source="auto", aliases=["梅梅"], summary="旧简介", confidence=0.6))
        await session.commit()

    await _merge(session_factory, name="韩梅梅", aliases=["梅梅", "小韩"], summary="新简介", role="主角", chapter_no=5)

    row = await _row(session_factory, "char-韩梅梅")
    assert json.loads(row.aliases_json) == ["梅梅", "小韩"]  # union, no dup
    assert row.summary == "新简介"
    assert row.role == "主角"
    assert row.last_seen_chapter == 5
    assert row.source == "auto"


@pytest.mark.asyncio
async def test_match_by_alias(session_factory):
    async with session_factory() as session:
        session.add(_seed_row("烛龙", source="user", aliases=["老龙"]))
        await session.commit()

    # Extraction reports the alias as the primary name: still merges into
    # the existing row instead of duplicating.
    await _merge(session_factory, name="老龙", aliases=[], summary="x", role="", chapter_no=9)

    async with session_factory() as session:
        from sqlalchemy import select

        rows = list((await session.scalars(select(CharacterModel))).all())
    assert len(rows) == 1
    assert rows[0].name == "烛龙"
    assert rows[0].last_seen_chapter == 9


@pytest.mark.asyncio
async def test_match_is_case_insensitive(session_factory):
    async with session_factory() as session:
        session.add(_seed_row("Sherlock", source="user", aliases=[]))
        await session.commit()

    await _merge(session_factory, name="sherlock", aliases=["Holmes"], summary="", role="", chapter_no=2)

    async with session_factory() as session:
        from sqlalchemy import select

        rows = list((await session.scalars(select(CharacterModel))).all())
    assert len(rows) == 1
    assert rows[0].last_seen_chapter == 2
