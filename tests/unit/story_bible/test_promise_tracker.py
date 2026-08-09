"""Promise lifecycle sync from goal hooks lines: plant/resolve parsing,
open -> developing on repeat, resolution with resolved_chapter, no-op for
hook-less goals, idempotency on chapter reruns."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.story_bible.promise_tracker import (
    parse_goal_hooks,
    sync_promises_from_goal,
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from tests.conftest import make_fk_engine

_TABLES = [ProjectModel.__table__, StoryBibleEntryModel.__table__]


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


def _promise(key: str, *, status: str = "open", value: dict | None = None) -> StoryBibleEntryModel:
    now = datetime.now(UTC)
    return StoryBibleEntryModel(
        id=f"promise-{key}", project_id="p1", kind="promise", key=key,
        value_json=json.dumps(value or {"note": key, "introduced_chapter": 1}, ensure_ascii=False),
        status=status, confidence=1.0, source="user", pinned=False, version=1,
        created_at=now, updated_at=now,
    )


async def _rows(session_factory) -> list[StoryBibleEntryModel]:
    async with session_factory() as session:
        return list((await session.scalars(select(StoryBibleEntryModel))).all())


def test_parse_goal_hooks_variants():
    # Batch deterministic line: colon form and bare-prefix form, multiple
    # separators, prefix inheritance within a segment group.
    assert parse_goal_hooks("写第3章\n伏笔/钩子：埋入：师父的遗言；回收：戒指的秘密\n目标字数：不少于 3000 字") == [
        ("plant", "师父的遗言"), ("resolve", "戒指的秘密"),
    ]
    assert parse_goal_hooks("伏笔/钩子：埋入A线索，B线索；照应C线索") == [
        ("plant", "A线索"), ("plant", "B线索"), ("resolve", "C线索"),
    ]
    assert parse_goal_hooks("伏笔/钩子：铺垫旧怨、暗示身世") == [
        ("plant", "旧怨"), ("plant", "身世"),
    ]
    # No hooks line -> no actions (single-chapter goals).
    assert parse_goal_hooks("写第3章《夜谈》") == []
    assert parse_goal_hooks("") == []
    # Bare clues without any directive prefix are not tracked.
    assert parse_goal_hooks("伏笔/钩子：师父的遗言") == []
    # Duplicate clue collapses.
    assert parse_goal_hooks("伏笔/钩子：埋入X，埋入X") == [("plant", "X")]


@pytest.mark.asyncio
async def test_plant_creates_open_promise(session_factory):
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=3,
            goal_text="伏笔/钩子：埋入：师父的遗言",
        )
        await session.commit()
    assert counts == {"planted": 1, "developed": 0, "resolved": 0}
    rows = await _rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "promise" and row.key == "师父的遗言" and row.status == "open"
    value = json.loads(row.value_json)
    assert value["introduced_chapter"] == 3


@pytest.mark.asyncio
async def test_replant_pushes_open_to_developing(session_factory):
    async with session_factory() as session:
        session.add(_promise("师父的遗言"))
        await session.commit()
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=5,
            goal_text="伏笔/钩子：埋入：师父的遗言",
        )
        await session.commit()
    assert counts == {"planted": 0, "developed": 1, "resolved": 0}
    rows = await _rows(session_factory)
    assert len(rows) == 1 and rows[0].status == "developing" and rows[0].version == 2


@pytest.mark.asyncio
async def test_resolve_open_goes_through_developing_to_resolved(session_factory):
    async with session_factory() as session:
        session.add(_promise("戒指的秘密"))
        await session.commit()
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=7,
            goal_text="伏笔/钩子：回收：戒指的秘密",
        )
        await session.commit()
    assert counts == {"planted": 0, "developed": 0, "resolved": 1}
    rows = await _rows(session_factory)
    assert len(rows) == 1 and rows[0].status == "resolved"
    value = json.loads(rows[0].value_json)
    assert value["resolved_chapter"] == 7
    assert value["introduced_chapter"] == 1  # older fields kept


@pytest.mark.asyncio
async def test_resolve_developing_directly(session_factory):
    async with session_factory() as session:
        session.add(_promise("旧怨", status="developing"))
        await session.commit()
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=9, goal_text="伏笔/钩子：照应旧怨",
        )
        await session.commit()
    assert counts["resolved"] == 1
    rows = await _rows(session_factory)
    assert rows[0].status == "resolved"


@pytest.mark.asyncio
async def test_goal_without_hooks_line_is_noop(session_factory):
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=2, goal_text="写第2章《山谷》",
        )
    assert counts == {"planted": 0, "developed": 0, "resolved": 0}
    assert await _rows(session_factory) == []


@pytest.mark.asyncio
async def test_rerun_same_chapter_never_duplicates(session_factory):
    goal = "伏笔/钩子：埋入：师父的遗言；回收：旧怨"
    async with session_factory() as session:
        session.add(_promise("旧怨", status="developing"))
        await session.commit()
    for _ in range(2):
        async with session_factory() as session:
            await sync_promises_from_goal(session, project_id="p1", chapter_no=4, goal_text=goal)
            await session.commit()
    rows = await _rows(session_factory)
    assert sorted(row.key for row in rows) == ["师父的遗言", "旧怨"]
    by_key = {row.key: row for row in rows}
    # First run planted open; the rerun only pushed it to developing.
    assert by_key["师父的遗言"].status == "developing"
    # Resolved rows are terminal: the rerun left them untouched.
    assert by_key["旧怨"].status == "resolved"
    assert by_key["旧怨"].version == 2


@pytest.mark.asyncio
async def test_terminal_and_excluded_promises_are_untouched(session_factory):
    async with session_factory() as session:
        session.add(_promise("已废弃", status="abandoned"))
        await session.commit()
    async with session_factory() as session:
        counts = await sync_promises_from_goal(
            session, project_id="p1", chapter_no=4, goal_text="伏笔/钩子：埋入：已废弃；回收：不存在的线索",
        )
        await session.commit()
    assert counts == {"planted": 0, "developed": 0, "resolved": 0}
    rows = await _rows(session_factory)
    assert len(rows) == 1 and rows[0].status == "abandoned"
