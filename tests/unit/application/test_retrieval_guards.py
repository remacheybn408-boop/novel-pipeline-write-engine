"""Retrieval guards: excluded facts and archived characters never
re-enter the scene pack / scene state / conflict matching; corrected
values take effect on the next build; draft versions are never indexed."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.indexing import OFF_IDENTITY, EmbeddingEngine
from proseforge.application.work.conflict_check import check_conflicts
from proseforge.application.work.retriever import NarrativeRetriever
from proseforge.application.work.scene_state import build_scene_state
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import ProviderCredentialModel
from proseforge.infrastructure.database.models.retrieval import (
    CanonConflictModel,
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
    RetrievalRunModel,
)
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine

_OFF_ENGINE = EmbeddingEngine(kind="off", identity=OFF_IDENTITY, embedder=None, max_chars=700)

_TABLES = [
    UserModel.__table__, ProjectModel.__table__, ChapterModel.__table__, ChapterVersionModel.__table__,
    CharacterModel.__table__, StoryBibleEntryModel.__table__,
    RetrievalDocumentModel.__table__, RetrievalChunkModel.__table__, RetrievalRunModel.__table__,
    RetrievalJobModel.__table__, CanonConflictModel.__table__,
    UserPreferenceModel.__table__, ProviderCredentialModel.__table__,
    RecapRollupModel.__table__,
]


@pytest.fixture(autouse=True)
def off_engine(monkeypatch):
    async def _off(uow, user_id, master_key):
        return _OFF_ENGINE

    monkeypatch.setattr("proseforge.application.work.retriever._resolve_embedding_engine", _off)


def _story(key: str, *, status: str, pinned: bool = False, kind: str = "world_rule", note: str = "设定") -> StoryBibleEntryModel:
    now = datetime.now(UTC)
    return StoryBibleEntryModel(
        id=f"sb-{key}-{status}", project_id="p1", kind=kind, key=key,
        value_json=json.dumps({"note": note}, ensure_ascii=False),
        status=status, confidence=1.0, source="user", pinned=pinned, version=1,
        created_at=now, updated_at=now,
    )


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="p1", owner_id="u1", slug="n", title="N"))
        await session.flush()
        session.add(_story("活跃规则", status="active", pinned=True, note="可见设定"))
        session.add(_story("被排除规则", status="excluded", pinned=True, note="不可见设定"))
        session.add(_story("已废弃伏笔", status="abandoned", kind="promise", note="不可见伏笔"))
        session.add(CharacterModel(id="c-active", project_id="p1", name="李雷", aliases_json="[]", summary="主角", role="主角", status="active", source="user", confidence=1.0, created_at=now, updated_at=now))
        session.add(CharacterModel(id="c-archived", project_id="p1", name="韩梅梅", aliases_json="[]", summary="退场", role="配角", status="archived", source="user", confidence=1.0, created_at=now, updated_at=now))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_excluded_facts_leave_scene_pack(session_factory):
    pack = await NarrativeRetriever(session_factory, master_key="k" * 32).build(
        project_id="p1", user_id="u1", query="设定", chapter_no=1
    )
    assert "活跃规则" in pack.sections["worldview"]
    assert "被排除规则" not in pack.sections["worldview"]
    assert "已废弃伏笔" not in pack.sections["worldview"]
    # Archived characters stay out too.
    assert "李雷" in pack.sections["worldview"]
    assert "韩梅梅" not in pack.sections["worldview"]


@pytest.mark.asyncio
async def test_excluded_facts_leave_scene_state(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")
    keys = [fact["key"] for fact in state["pinned_facts"]]
    assert "活跃规则" in keys
    assert "被排除规则" not in keys
    assert "已废弃伏笔" not in keys


@pytest.mark.asyncio
async def test_excluded_fact_not_compared_in_conflict_check(session_factory):
    facts = [{"entity": "被排除规则", "field": "地点", "value": "山谷"}]
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        written = await check_conflicts(uow, project_id="p1", version_id="v1", chapter_no=1, facts=facts)
    assert written == 0  # the excluded row is not a comparison target


@pytest.mark.asyncio
async def test_archived_character_not_compared_in_conflict_check(session_factory):
    facts = [{"entity": "韩梅梅", "field": "角色", "value": "反派"}]
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        written = await check_conflicts(uow, project_id="p1", version_id="v1", chapter_no=1, facts=facts)
    assert written == 0  # archived characters are skipped


@pytest.mark.asyncio
async def test_corrected_value_wins_on_next_build(session_factory):
    async with session_factory() as session:
        row = await session.get(StoryBibleEntryModel, "sb-活跃规则-active")
        row.value_json = json.dumps({"note": "纠正后的设定"}, ensure_ascii=False)
        row.version = 2
        await session.commit()
    pack = await NarrativeRetriever(session_factory, master_key="k" * 32).build(
        project_id="p1", user_id="u1", query="设定", chapter_no=1
    )
    assert "纠正后的设定" in pack.sections["worldview"]
    assert "可见设定" not in pack.sections["worldview"]


@pytest.mark.asyncio
async def test_draft_version_produces_no_chunks(session_factory, monkeypatch):
    """Indexing keys on the ACTIVE version: a draft (never activated) is
    invisible to retrieval."""
    from proseforge.application.retrieval.indexing import execute_index_job

    async def _off_engine(uow, user_id, master_key):
        return _OFF_ENGINE

    monkeypatch.setattr("proseforge.application.retrieval.indexing._resolve_embedding_engine", _off_engine)

    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(ChapterModel(id="ch1", project_id="p1", chapter_no=1, title="第一章", status="DONE", active_version_id="v1"))
        session.add(ChapterVersionModel(id="v1", chapter_id="ch1", version_no=1, content="已采纳的正文", content_hash="h1", word_count=6, summary=""))
        await session.flush()
        session.add(RetrievalJobModel(id="j1", project_id="p1", job_type="index_chapter", source_type="chapter", source_id="ch1", status="pending", attempt=0, requested_at=now))
        await session.commit()

    result = await execute_index_job({"job_id": "j1", "user_id": "u1"}, session_factory, master_key="k" * 32)
    assert result == "done"

    # A draft version appears but is never set active; re-indexing is a
    # no-op and the draft text never reaches retrieval_chunks.
    async with session_factory() as session:
        session.add(ChapterVersionModel(id="v2-draft", chapter_id="ch1", version_no=2, content="未确认的草稿内容", content_hash="h2", word_count=8, summary=""))
        session.add(RetrievalJobModel(id="j2", project_id="p1", job_type="index_chapter", source_type="chapter", source_id="ch1", status="pending", attempt=0, requested_at=now))
        await session.commit()
    result = await execute_index_job({"job_id": "j2", "user_id": "u1"}, session_factory, master_key="k" * 32)
    assert result == "skipped"

    async with session_factory() as session:
        chunks = list((await session.scalars(select(RetrievalChunkModel))).all())
    assert len(chunks) == 1
    assert chunks[0].content == "已采纳的正文"
    assert "未确认的草稿内容" not in chunks[0].content
