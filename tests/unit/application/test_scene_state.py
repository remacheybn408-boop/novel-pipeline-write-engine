"""Scene-state assembly: latest chapter, recent summaries, matched
characters, pinned facts, open promises, and the character budget."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.work.scene_state import CHAR_BUDGET, build_scene_state
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine

_TABLES = [
    ProjectModel.__table__, ChapterModel.__table__, ChapterVersionModel.__table__,
    CharacterModel.__table__, StoryBibleEntryModel.__table__, RecapRollupModel.__table__,
]


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


def _seed_chapter(no: int, title: str, content: str, summary: str) -> tuple[ChapterModel, ChapterVersionModel]:
    chapter = ChapterModel(id=f"ch{no}", project_id="p1", chapter_no=no, title=title, status="DONE", active_version_id=f"v{no}")
    version = ChapterVersionModel(id=f"v{no}", chapter_id=f"ch{no}", version_no=1, content=content, content_hash=f"h{no}", word_count=len(content), summary=summary)
    return chapter, version


def _story(key: str, *, kind: str = "world_rule", pinned: bool = False, status: str = "active", value: dict | None = None) -> StoryBibleEntryModel:
    now = datetime.now(UTC)
    return StoryBibleEntryModel(
        id=f"fact-{kind}-{key}", project_id="p1", kind=kind, key=key,
        value_json=json.dumps(value or {"note": key}, ensure_ascii=False),
        status=status, confidence=1.0, source="user", pinned=pinned, version=1,
        created_at=now, updated_at=now,
    )


async def _seed_base(session_factory) -> None:
    async with session_factory() as session:
        for no in range(1, 8):
            chapter, version = _seed_chapter(no, f"第{no}章", f"第{no}章正文，李雷出场。" * 3, f"第{no}章摘要")
            session.add_all([chapter, version])
        now = datetime.now(UTC)
        session.add(CharacterModel(
            id="c1", project_id="p1", name="李雷", aliases_json='["雷子"]', summary="主角",
            role="主角", status="active", source="user", confidence=1.0, created_at=now, updated_at=now,
        ))
        session.add(CharacterModel(
            id="c2", project_id="p1", name="韩梅梅", aliases_json="[]", summary="同学",
            role="配角", status="active", source="user", confidence=1.0, created_at=now, updated_at=now,
        ))
        session.add(_story("重力规则", pinned=True))
        session.add(_story("三日之约", kind="promise", status="open"))
        session.add(_story("已兑现", kind="promise", status="fulfilled"))
        session.add(_story("普通条目"))
        await session.commit()


@pytest.mark.asyncio
async def test_scene_state_full_assembly(session_factory):
    await _seed_base(session_factory)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")

    assert state["latest_chapter"] == {"no": 7, "title": "第7章", "summary": "第7章摘要"}
    assert [item["no"] for item in state["recent_summaries"]] == [7, 6, 5, 4, 3]  # last 5, desc
    # Latest chapter content mentions 李雷 (and alias 雷子 absent), not 韩梅梅.
    assert [c["name"] for c in state["characters"]] == ["李雷"]
    assert [f["key"] for f in state["pinned_facts"]] == ["重力规则"]
    assert [p["key"] for p in state["open_promises"]] == ["三日之约"]


@pytest.mark.asyncio
async def test_scene_state_empty_project(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")
    assert state["latest_chapter"] is None
    assert state["recent_summaries"] == []
    assert state["characters"] == []
    assert state["pinned_facts"] == []
    assert state["open_promises"] == []


@pytest.mark.asyncio
async def test_budget_drops_oldest_summaries_first(session_factory):
    big = "长" * 1500
    async with session_factory() as session:
        for no in range(1, 6):
            chapter, version = _seed_chapter(no, f"第{no}章", "正文", big)
            session.add_all([chapter, version])
        await session.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")
    total = len(state["latest_chapter"]["summary"]) + sum(len(i["summary"]) for i in state["recent_summaries"])
    assert total <= CHAR_BUDGET + len(big)  # latest may be hard-truncated last
    # Newest summaries survive; oldest dropped first.
    nos = [item["no"] for item in state["recent_summaries"]]
    assert nos == sorted(nos, reverse=True)
    assert nos[0] == 5
    assert len(state["recent_summaries"]) < 5


@pytest.mark.asyncio
async def test_state_ledger_blocks_in_scene_state(session_factory):
    await _seed_base(session_factory)
    async with session_factory() as session:
        session.add(_story(
            "李雷", kind="character_state",
            value={"emotion": "焦虑", "mental": "失眠", "note": "刚得知真相", "chapter_no": 7},
        ))
        for no in (5, 6, 7):
            session.add(_story(
                f"ch{no}", kind="chapter_fact",
                value={"chapter_no": no, "timeline": f"第{no}章时间线", "items": {"戒指": "李雷"}, "revealed": [f"揭示{no}"]},
            ))
        await session.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")

    assert state["character_states"] == [
        {"key": "李雷", "value": {"emotion": "焦虑", "mental": "失眠", "note": "刚得知真相", "chapter_no": 7}}
    ]
    # Only the newest two chapter facts are kept, newest first.
    assert [fact["chapter_no"] for fact in state["chapter_facts"]] == [7, 6]


@pytest.mark.asyncio
async def test_budget_drops_ledger_blocks_before_summaries(session_factory):
    big = "长" * 1500
    async with session_factory() as session:
        for no in range(1, 6):
            chapter, version = _seed_chapter(no, f"第{no}章", "正文", big)
            session.add_all([chapter, version])
        session.add(_story("李雷", kind="character_state", value={"emotion": "焦虑", "note": "注" * 500}))
        session.add(_story("ch5", kind="chapter_fact", value={"chapter_no": 5, "timeline": "线" * 500}))
        await session.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")
    # Over budget: ledger blocks are dropped before recent summaries.
    assert state["chapter_facts"] == [] and state["character_states"] == []
    assert len(state["recent_summaries"]) >= 1
