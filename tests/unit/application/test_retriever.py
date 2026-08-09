"""NarrativeRetriever: four-section pack, switch semantics, snapshot row,
evidence budget. Embedding engine stubbed to the off tier (keyword-only)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.indexing import OFF_IDENTITY, EmbeddingEngine
from proseforge.application.work.retriever import (
    EVIDENCE_BUDGET_TOKENS,
    NARRATIVE_RAG_SKILL_KEY,
    QUERY_MAX_CHARS,
    NarrativeRetriever,
    _format_character,
    narrative_rag_switch_enabled,
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.plugin import (
    UserBuiltinSkillStateModel,
    UserPreferenceModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import (
    AuditLogModel,
    ProviderCredentialModel,
)
from proseforge.infrastructure.database.models.retrieval import (
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
    UserModel.__table__,
    ProjectModel.__table__, ChapterModel.__table__, ChapterVersionModel.__table__,
    CharacterModel.__table__, StoryBibleEntryModel.__table__,
    RetrievalDocumentModel.__table__, RetrievalChunkModel.__table__, RetrievalRunModel.__table__,
    UserPreferenceModel.__table__, ProviderCredentialModel.__table__, UserBuiltinSkillStateModel.__table__,
    RecapRollupModel.__table__, RetrievalJobModel.__table__, AuditLogModel.__table__,
]


@pytest.fixture(autouse=True)
def off_engine(monkeypatch):
    async def _off(uow, user_id, master_key):
        return _OFF_ENGINE

    monkeypatch.setattr("proseforge.application.work.retriever._resolve_embedding_engine", _off)


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
        session.add(ChapterModel(id="ch1", project_id="p1", chapter_no=2, title="第二章", status="DONE", active_version_id="v1"))
        session.add(ChapterVersionModel(id="v1", chapter_id="ch1", version_no=1, content="烛龙现身，李雷迎战。", content_hash="h", word_count=10, summary="李雷迎战烛龙"))
        session.add(CharacterModel(id="c1", project_id="p1", name="李雷", aliases_json='["雷子"]', summary="主角", role="主角", status="active", source="user", confidence=1.0, created_at=now, updated_at=now))
        session.add(CharacterModel(id="c2", project_id="p1", name="烛龙", aliases_json="[]", summary="神兽", role="反派", status="active", source="auto", confidence=0.6, created_at=now, updated_at=now))
        session.add(StoryBibleEntryModel(id="sb1", project_id="p1", kind="world_rule", key="重力规则", value_json='{"note": "睁眼为昼"}', status="active", confidence=1.0, source="user", pinned=True, version=1, created_at=now, updated_at=now))
        session.add(StoryBibleEntryModel(id="sb2", project_id="p1", kind="promise", key="三日之约", value_json='{"note": "必须兑现"}', status="open", confidence=1.0, source="user", pinned=False, version=1, created_at=now, updated_at=now))
        session.add(RetrievalDocumentModel(id="d1", project_id="p1", source_type="chapter", source_id="ch1", source_version="v0", title="第一章", status="active", authority_level="canon", chapter_from=1, chapter_to=1, created_at=now, updated_at=now))
        await session.flush()
        session.add(RetrievalChunkModel(id="ck0", project_id="p1", document_id="d1", chunk_index=0, content="李雷在第一章偶遇烛龙踪迹", summary="", metadata_json='{"chapter_no": 1}', search_text="x", embedding=None, embedding_model="none", embedding_version="v1", token_count=10, content_hash="h0", status="active", created_at=now, updated_at=now))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_scene_pack_four_sections_and_snapshot(session_factory):
    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷 烛龙", chapter_no=3)

    assert "[世界观与设定]" in pack.text
    assert "重力规则" in pack.sections["worldview"]  # pinned first
    assert pack.sections["worldview"].index("重力规则") < pack.sections["worldview"].index("李雷")
    # user-sourced characters before auto.
    assert pack.sections["worldview"].index("李雷") < pack.sections["worldview"].index("烛龙")
    assert "[当前状态]" in pack.text and "第2章" in pack.sections["current_state"]
    assert "三日之约" in pack.sections["current_state"]  # open promise
    assert "[写作约束]" in pack.text and "第3章" in pack.sections["constraints"]
    # Keyword leg found the 烛龙 chunk, annotated with chapter number.
    assert "[长篇事实证据]" in pack.text
    assert "【第1章】" in pack.sections["evidence"]
    assert pack.evidence[0].chunk_id == "ck0"

    async with session_factory() as session:
        runs = list((await session.scalars(select(RetrievalRunModel))).all())
    assert len(runs) == 1
    run = runs[0]
    assert run.id == pack.run_id
    assert run.query_text == "李雷 烛龙"
    assert run.intent == "scene_pack"
    selected = json.loads(run.selected_chunks_json)
    # Snapshot shape: {chunks, trimmed, budget}.
    assert selected["chunks"][0]["chunk_id"] == "ck0" and selected["chunks"][0]["chapter_no"] == 1
    assert selected["trimmed"] == []
    assert selected["budget"]["structured_tokens"] > 0 and selected["budget"]["token_cost"] == pack.token_cost
    assert run.elapsed_ms >= 0 and run.token_cost == pack.token_cost


@pytest.mark.asyncio
async def test_switch_default_on_and_row_overrides(session_factory):
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await narrative_rag_switch_enabled(uow, "u1") is True  # no row -> ON
        assert await narrative_rag_switch_enabled(uow, "") is False
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserBuiltinSkillStateModel(id="st1", user_id="u1", skill_key=NARRATIVE_RAG_SKILL_KEY, enabled=False, created_at=now))
        await session.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await narrative_rag_switch_enabled(uow, "u1") is False


@pytest.mark.asyncio
async def test_evidence_budget_trims_low_score_blocks(session_factory):
    now = datetime.now(UTC)
    async with session_factory() as session:
        # 30 chunks, each well over budget/8 -> only a few fit.
        for index in range(30):
            session.add(RetrievalChunkModel(
                id=f"big{index}", project_id="p1", document_id="d1", chunk_index=index + 10,
                content=f"李雷烛龙{'长' * 3000}", summary="", metadata_json='{"chapter_no": 1}',
                search_text="x", embedding=None, embedding_model="none", embedding_version="v1",
                token_count=10, content_hash=f"hb{index}", status="active", created_at=now, updated_at=now,
            ))
        await session.commit()
    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷 烛龙", chapter_no=3)
    estimated = sum(max(1, len(line) // 2) for line in pack.sections["evidence"].split("\n\n"))
    assert estimated <= EVIDENCE_BUDGET_TOKENS + 2000  # one block granularity
    assert len(pack.evidence) <= 12  # evidence cap (8 fused + neighbors)


class _RecordingEmbedder:
    """Stands in for a local embedder: records which side (query vs passage)
    each text was embedded on, returns a fixed-dimension vector."""

    def __init__(self):
        self.query_inputs: list[str] = []
        self.passage_inputs: list[str] = []

    async def embed(self, texts):
        self.passage_inputs.extend(texts)
        return SimpleNamespace(vectors=[[0.1, 0.2]] * len(texts), total_tokens=1, truncated=[])

    async def embed_query(self, texts):
        self.query_inputs.extend(texts)
        return SimpleNamespace(vectors=[[0.1, 0.2]] * len(texts), total_tokens=1, truncated=[])


@pytest.mark.asyncio
async def test_query_vectorized_via_embed_query(session_factory, monkeypatch):
    """Regression (M12): the retrieval query must go through embed_query —
    for e5-family local models embed() applies the passage prefix, which
    silently degrades retrieval quality."""
    embedder = _RecordingEmbedder()

    async def _local_engine(uow, user_id, master_key):
        # identity "none" matches this fixture's seeded chunks (the keyword
        # leg filters by engine identity; a foreign identity is excluded by
        # design — mid-switch chunks must not leak into the RRF).
        return EmbeddingEngine(kind="local", identity="none", embedder=embedder, max_chars=700)

    # Overrides the autouse off-engine stub.
    monkeypatch.setattr("proseforge.application.work.retriever._resolve_embedding_engine", _local_engine)

    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷 烛龙", chapter_no=3)

    assert embedder.query_inputs == ["李雷 烛龙"]
    assert embedder.passage_inputs == []
    assert pack.sections["evidence"]  # keyword leg still produced evidence


@pytest.mark.asyncio
async def test_query_capped_at_max_chars(session_factory):
    """A pasted full chapter as the query must not flow unbounded into the
    pack's constraints section or the persisted retrieval_runs row."""
    long_query = "李雷 " + "长" * 5000
    pack = await NarrativeRetriever(session_factory, master_key="k" * 32).build(
        project_id="p1", user_id="u1", query=long_query, chapter_no=3
    )

    goal_line = next(
        line for line in pack.sections["constraints"].splitlines() if line.startswith("- 本章目标：")
    )
    assert len(goal_line.removeprefix("- 本章目标：")) == QUERY_MAX_CHARS

    async with session_factory() as session:
        runs = list((await session.scalars(select(RetrievalRunModel))).all())
    assert len(runs) == 1
    assert len(runs[0].query_text) <= QUERY_MAX_CHARS


@pytest.mark.asyncio
async def test_voice_profile_and_state_ledger_rendering(session_factory):
    now = datetime.now(UTC)
    voice = {
        "sentence_len": [5, 15], "connectors": ["然后"], "banned_words": [],
        "emotion_baseline": "外放", "register": "口语",
        "dialect": "川普", "catchphrases": ["巴适", "要得"],
    }
    async with session_factory() as session:
        session.add(StoryBibleEntryModel(
            id="sb-char", project_id="p1", kind="character", key="李雷",
            value_json=json.dumps({"note": "主角设定", "voice": voice}, ensure_ascii=False),
            status="active", confidence=1.0, source="user", pinned=False, version=1,
            created_at=now, updated_at=now,
        ))
        session.add(StoryBibleEntryModel(
            id="sb-state", project_id="p1", kind="character_state", key="李雷",
            value_json=json.dumps({"emotion": "焦虑", "mental": "失眠", "note": "刚得知真相", "chapter_no": 2}, ensure_ascii=False),
            status="active", confidence=0.6, source="auto", pinned=False, version=1,
            created_at=now, updated_at=now,
        ))
        session.add(StoryBibleEntryModel(
            id="sb-fact", project_id="p1", kind="chapter_fact", key="ch2",
            value_json=json.dumps({"chapter_no": 2, "timeline": "当夜", "items": {"戒指": "李雷"}, "revealed": ["师父的身份"]}, ensure_ascii=False),
            status="active", confidence=0.6, source="auto", pinned=False, version=1,
            created_at=now, updated_at=now,
        ))
        await session.commit()

    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷 烛龙", chapter_no=3)

    # Voice profile rides the character line in worldview.
    character_line = next(line for line in pack.sections["worldview"].splitlines() if line.startswith("- 李雷"))
    assert "声纹：" in character_line and "句式5-15字" in character_line
    assert "方言：川普" in character_line
    assert "口头禅：巴适、要得" in character_line
    assert "情绪基线：外放" in character_line
    # Ledger kinds never appear as worldview entries...
    assert "[chapter_fact]" not in pack.sections["worldview"]
    assert "[character_state]" not in pack.sections["worldview"]
    # ...they render in the current-state section instead.
    current_state = pack.sections["current_state"]
    assert "角色精神状态：李雷：焦虑/失眠（刚得知真相）" in current_state
    assert "第2章事实：" in current_state
    assert "时间线：当夜" in current_state
    assert "道具：戒指→李雷" in current_state
    assert "揭示：师父的身份" in current_state


def test_format_character_without_voice_keeps_legacy_shape():
    character = SimpleNamespace(name="李雷", aliases=["雷子"], role="主角", summary="主角", source="user")
    assert _format_character(character) == "- 李雷（别名：雷子）｜主角：主角"
    assert _format_character(character, {}) == "- 李雷（别名：雷子）｜主角：主角"
    partial = _format_character(character, {"dialect": "粤语"})
    assert partial.endswith("（声纹：方言：粤语）")


# -- memory pyramid wiring (phase-2 items 8/9) ---------------------------------


def _recap_rollup(rollup_id: str, level: str, span_start: int, span_end: int, content: str, *, stale: bool = False) -> RecapRollupModel:
    now = datetime.now(UTC)
    return RecapRollupModel(
        id=rollup_id, project_id="p1", user_id="u1", level=level,
        span_start=span_start, span_end=span_end, content=content,
        source_version_ids="[]", stale=stale, created_at=now, updated_at=now,
    )


@pytest.mark.asyncio
async def test_scene_pack_injects_settled_recaps_but_never_stale(session_factory):
    async with session_factory() as session:
        session.add(_recap_rollup("roll-fresh", "volume", 1, 2, "前两卷主线：李雷拜师。"))
        session.add(_recap_rollup("roll-stale", "book", 1, 2, "过期的全书梗概内容", stale=True))
        await session.commit()

    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷", chapter_no=3)

    current_state = pack.sections["current_state"]
    assert "卷梗概（第1-2章）：前两卷主线：李雷拜师。" in current_state
    assert "过期的全书梗概内容" not in current_state


@pytest.mark.asyncio
async def test_scene_pack_canon_evidence_outranks_derived_recap(session_factory):
    """Authority 分层接线：衍生梗概块即便关键词得分更高，融合后仍排在
    canon 原文块之后（衍生梗概永不盖过原文）。"""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(RetrievalDocumentModel(
            id="d-recap", project_id="p1", source_type="recap_rollup", source_id="roll-1",
            source_version="hash-1", title="卷梗概（第1-2章）", status="active",
            authority_level="derived", chapter_from=1, chapter_to=2, created_at=now, updated_at=now,
        ))
        await session.flush()
        # Derived chunk: entity-boosted (李雷 + 烛龙 hits), would win plain RRF.
        session.add(RetrievalChunkModel(
            id="ckR", project_id="p1", document_id="d-recap", chunk_index=0,
            content="李雷迎战烛龙的卷梗概，李雷与烛龙立下约定", summary="", metadata_json="{}",
            search_text="x", embedding=None, embedding_model="none", embedding_version="v1",
            token_count=10, content_hash="hR", status="active", created_at=now, updated_at=now,
        ))
        await session.commit()

    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷 烛龙", chapter_no=3)

    hit_ids = [block.chunk_id for block in pack.evidence if not block.expanded]
    assert "ck0" in hit_ids and "ckR" in hit_ids
    assert hit_ids.index("ck0") < hit_ids.index("ckR")  # canon before derived
    # The recap block is labelled by document title, not 第N章.
    recap_line = next(line for line in pack.sections["evidence"].split("\n\n") if "卷梗概" in line)
    assert recap_line.startswith("【卷梗概（第1-2章）】")


@pytest.mark.asyncio
async def test_scene_pack_triggers_lazy_recap_recompute(session_factory):
    """开写前的惰性重算：本卷梗概 stale → 建包时入队 rollup_recap 重算任务
    （source=该卷末章 ch1，chapter_no=2）+ recap.recompute_queued 审计。"""
    async with session_factory() as session:
        session.add(_recap_rollup("roll-v1", "volume", 1, 2, "过期卷梗概", stale=True))
        await session.commit()

    retriever = NarrativeRetriever(session_factory, master_key="k" * 32)
    pack = await retriever.build(project_id="p1", user_id="u1", query="李雷", chapter_no=3)
    assert pack.text  # the pack itself built fine (stale recap excluded above)

    async with session_factory() as session:
        jobs = list((await session.scalars(
            select(RetrievalJobModel).where(RetrievalJobModel.job_type == "rollup_recap")
        )).all())
        audits = list((await session.scalars(
            select(AuditLogModel).where(AuditLogModel.action == "recap.recompute_queued")
        )).all())
    assert len(jobs) == 1
    assert jobs[0].source_type == "chapter" and jobs[0].source_id == "ch1"
    assert jobs[0].status in {"pending", "running"}  # dispatch is best-effort
    assert len(audits) == 1 and json.loads(audits[0].payload)["trigger_chapter"] == 3
