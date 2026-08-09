"""跨章接缝卡（application/agents/seam_card.py）测试。

sqlite 真实落库（make_fk_engine，同 test_summarize_chapter 模式）：
- 有前章：卡含结构化锚点（时间锚/未结伏笔/人物位置）+ 上章摘要 + 结尾原文
- 首章/无前章：空卡
- 前章摘要未落库：卡照出（结尾原文兜底），lagging=True（摘要滞后告警）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.agents.seam_card import load_seam_card
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine

_TABLES = [
    UserModel.__table__, ProjectModel.__table__, ChapterModel.__table__, ChapterVersionModel.__table__, StoryBibleEntryModel.__table__,
]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_prev_chapter(session_factory, *, summary: str = "李雷与烛龙定下三日之约。", with_fact: bool = True) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        await session.flush()  # FK parents first: relationship-less tables flush in arbitrary order
        session.add(ChapterModel(id="ch1", project_id="p1", chapter_no=1, title="第一章", status="DONE", active_version_id="v1"))
        ending = "烛龙离去前回头望了一眼，山谷里只剩下李雷和他的戒指。" + "尾" * 900
        session.add(ChapterVersionModel(id="v1", chapter_id="ch1", version_no=1, content=ending, content_hash="h1", word_count=500, summary=summary))
        if with_fact:
            session.add(StoryBibleEntryModel(
                id="sb1", project_id="p1", kind="chapter_fact", key="ch1",
                value_json=json.dumps({
                    "chapter_no": 1, "time_anchor": "当夜", "open_loops": ["戒指的来历", "师父的身份"],
                    "locations": {"李雷": "山谷"}, "revealed": ["三日之约"],
                }, ensure_ascii=False),
                status="active", confidence=0.6, source="auto", pinned=False, version=1,
                created_at=now, updated_at=now,
            ))
        await session.commit()


def _context(session_factory, project_id: str = "p1") -> dict[str, object]:
    return {"uow_factory": lambda: SqlAlchemyUnitOfWork(session_factory), "run": {"id": "run-1", "project_id": project_id}}


@pytest.mark.asyncio
async def test_seam_card_renders_anchors_summary_and_ending(session_factory):
    await _seed_prev_chapter(session_factory)

    card, lagging = await load_seam_card(_context(session_factory), "写第2章《第二章》")

    assert not lagging
    assert "第1章《第一章》" in card
    assert "时间锚：当夜" in card
    assert "未结伏笔：戒指的来历；师父的身份" in card
    assert "人物位置：李雷→山谷" in card
    assert "上章摘要：李雷与烛龙定下三日之约。" in card
    assert "上章结尾原文：" in card
    # 结尾摘录只取末尾 800 字：开头的「烛龙离去」被截掉，尾部在场
    assert "烛龙离去前回头望了一眼" not in card
    assert "尾" * 100 in card


@pytest.mark.asyncio
async def test_seam_card_first_chapter_is_empty(session_factory):
    await _seed_prev_chapter(session_factory)

    card, lagging = await load_seam_card(_context(session_factory), "写第1章《第一章》")

    assert card == "" and not lagging


@pytest.mark.asyncio
async def test_seam_card_without_summary_marks_lagging(session_factory):
    await _seed_prev_chapter(session_factory, summary="")

    card, lagging = await load_seam_card(_context(session_factory), "写第2章")

    assert lagging  # 摘要滞后告警由调用方记 context.summary_lagging
    assert "上章摘要：" not in card
    assert "上章结尾原文：" in card  # 结尾原文兜底，卡照出


@pytest.mark.asyncio
async def test_seam_card_without_uow_factory_is_empty():
    card, lagging = await load_seam_card({"run": {"id": "r", "project_id": "p1"}}, "写第2章")
    assert card == "" and not lagging
