"""Conflict detection v1: comparison matrix, normalization, dedupe."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.work.conflict_check import (
    canonical_field,
    check_conflicts,
    find_conflicts,
    normalize_text,
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import CanonConflictModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine


def _character(name: str, role: str, aliases: list[str] | None = None, cid: str = "c1"):
    return SimpleNamespace(id=cid, name=name, role=role, aliases=aliases or [])


def _story(key: str, value: dict, sid: str = "s1"):
    return SimpleNamespace(id=sid, key=key, value_json=json.dumps(value, ensure_ascii=False))


def test_normalize_text():
    assert normalize_text(" 主角 ") == "主角"
    assert normalize_text("Sherlock Holmes") == "sherlockholmes"
    assert normalize_text("半角ｶﾀｶﾅ") == normalize_text("半角カタカナ".replace("ｶﾀｶﾅ", "カタカナ"))
    assert normalize_text("A，B。C") == "abc"


def test_canonical_field_mapping():
    assert canonical_field("角色") == "role"
    assert canonical_field("身份定位") == "role"
    assert canonical_field("role") == "role"
    assert canonical_field("别名") == "alias"
    assert canonical_field("所在地") == "location"
    assert canonical_field("身高") is None
    assert canonical_field("") is None


def test_same_value_no_conflict():
    facts = [{"entity": "李雷", "field": "角色", "value": "主角"}]
    assert find_conflicts(facts, [_character("李雷", "主角")], []) == []


def test_different_value_conflicts():
    facts = [{"entity": "李雷", "field": "角色", "value": "反派"}]
    conflicts = find_conflicts(facts, [_character("李雷", "主角")], [])
    assert len(conflicts) == 1
    assert conflicts[0].conflicting_source == "character:c1"
    assert conflicts[0].candidate_value == "反派"
    assert conflicts[0].existing_value == "主角"


def test_prose_values_not_compared():
    long_value = "散" * 60
    facts = [{"entity": "李雷", "field": "角色", "value": long_value}]
    assert find_conflicts(facts, [_character("李雷", "主角")], []) == []
    # Existing prose is not compared either.
    facts = [{"entity": "李雷", "field": "角色", "value": "主角"}]
    assert find_conflicts(facts, [_character("李雷", "散" * 60)], []) == []


def test_entity_matched_via_alias_and_normalization():
    facts = [{"entity": " 雷子 ", "field": "身份", "value": "反派"}]
    conflicts = find_conflicts(facts, [_character("李雷", "主角", aliases=["雷子"])], [])
    assert len(conflicts) == 1


def test_story_bible_structured_field_conflict():
    facts = [{"entity": "烛龙", "field": "状态", "value": "沉睡"}]
    rows = [_story("烛龙", {"status": "苏醒", "note": "x"})]
    conflicts = find_conflicts(facts, [], rows)
    assert len(conflicts) == 1
    assert conflicts[0].conflicting_source == "story_bible:s1"
    assert conflicts[0].existing_value == "苏醒"
    # Same value -> quiet.
    facts = [{"entity": "烛龙", "field": "状态", "value": "苏醒"}]
    assert find_conflicts(facts, [], rows) == []


def test_unknown_field_ignored():
    facts = [{"entity": "李雷", "field": "身高", "value": "一米九"}]
    assert find_conflicts(facts, [_character("李雷", "主角")], []) == []


def test_duplicate_facts_deduped_in_memory():
    facts = [
        {"entity": "李雷", "field": "角色", "value": "反派"},
        {"entity": "李雷", "field": "角色", "value": "反派"},
    ]
    assert len(find_conflicts(facts, [_character("李雷", "主角")], [])) == 1


_TABLES = [
    ProjectModel.__table__, CharacterModel.__table__, StoryBibleEntryModel.__table__, CanonConflictModel.__table__,
]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(ProjectModel(id="p1", owner_id="u1", slug="n", title="N"))
        await session.flush()
        session.add(CharacterModel(id="c1", project_id="p1", name="李雷", aliases_json="[]", summary="", role="主角", status="active", source="user", confidence=1.0, created_at=now, updated_at=now))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_check_conflicts_writes_and_dedupes_open_rows(session_factory):
    facts = [{"entity": "李雷", "field": "角色", "value": "反派"}]
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        written = await check_conflicts(uow, project_id="p1", version_id="v1", chapter_no=3, facts=facts)
        await uow.commit()
    assert written == 1

    # Second identical run: the open row suppresses duplicates.
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        written = await check_conflicts(uow, project_id="p1", version_id="v1", chapter_no=3, facts=facts)
        await uow.commit()
    assert written == 0

    async with session_factory() as session:
        rows = list((await session.scalars(select(CanonConflictModel))).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.candidate_source == "chapter_version:v1"
    assert row.conflicting_source == "character:c1"
    assert row.status == "open"
    evidence = json.loads(row.evidence_json)
    assert evidence == {"candidate_value": "反派", "existing_value": "主角", "chapter_no": 3}
