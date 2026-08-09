"""summarize_chapter job: happy path, idempotency, defensive parse,
missing-config failure, retry chain. Provider fully stubbed."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.work.summarize_chapter import (
    execute_summarize_job,
    parse_summary_payload,
)
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import AgentMemoryModel
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import ProviderCredentialModel
from proseforge.infrastructure.database.models.retrieval import (
    CanonConflictModel,
    RetrievalJobModel,
)
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from tests.conftest import make_fk_engine

MASTER_KEY = base64.b64encode(b"k" * 32).decode()

_TABLES = [
    UserModel.__table__, ProjectModel.__table__, ChapterModel.__table__, ChapterVersionModel.__table__,
    ProviderCredentialModel.__table__, UserPreferenceModel.__table__, RetrievalJobModel.__table__,
    CharacterModel.__table__, StoryBibleEntryModel.__table__, CanonConflictModel.__table__,
    AgentMemoryModel.__table__,
]


class StubProvider:
    provider_id = "openai"

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text = text
        self.error = error
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        yield GenerationEvent("content.delta", self.text or "")


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _payload(user_id: str, provider: str, cred_id: str) -> str:
    encrypted = CredentialCipher(derive_key(MASTER_KEY)).encrypt(
        json.dumps({"api_key": "sk-test"}).encode(), associated_data=f"{user_id}:{provider}:{cred_id}".encode()
    )
    return base64.b64encode(encrypted).decode()


async def _seed(session_factory, *, with_credential: bool = True, version_summary: str = "", lock: bool = True) -> str:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        await session.flush()
        project = await session.get(ProjectModel, "p1")
        if lock:
            project.writing_model_provider = "openai"
            project.writing_model_id = "gpt-lock"
            project.model_locked_at = now
            project.model_lock_source = "first_chapter"
        if with_credential:
            session.add(ProviderCredentialModel(id="cred-1", user_id="u1", provider="openai", encrypted_payload=_payload("u1", "openai", "cred-1")))
        session.add(ChapterModel(id="ch1", project_id="p1", chapter_no=2, title="第二章", status="DONE", active_version_id="v1"))
        session.add(ChapterVersionModel(id="v1", chapter_id="ch1", version_no=1, content="烛龙现身，李雷迎战。", content_hash="h1", word_count=12, summary=version_summary))
        session.add(RetrievalJobModel(
            id="job-1", project_id="p1", job_type="summarize_chapter", source_type="chapter_version",
            source_id="v1", status="pending", attempt=0, requested_at=now,
        ))
        await session.commit()
    return "job-1"


def _patch_provider(monkeypatch, provider: StubProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _job(session_factory, job_id: str) -> RetrievalJobModel:
    async with session_factory() as session:
        return await session.get(RetrievalJobModel, job_id)


GOOD_JSON = json.dumps({
    "summary": "烛龙现身山谷，李雷被迫迎战，二人定下三日之约。",
    "characters": [{"name": "烛龙", "aliases": ["老龙"], "summary": "上古神兽", "role": "反派"}],
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_happy_path_writes_summary_and_characters(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    provider = StubProvider(text=f"```json\n{GOOD_JSON}\n```")
    _patch_provider(monkeypatch, provider)

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "characters": 1, "conflicts": 0}
    # The locked model was used, not any default.
    assert provider.requests[0].model == "gpt-lock"
    async with session_factory() as session:
        version = await session.get(ChapterVersionModel, "v1")
        assert version.summary.startswith("烛龙现身山谷")
        characters = list((await session.scalars(select(CharacterModel))).all())
    assert len(characters) == 1
    assert characters[0].name == "烛龙"
    assert characters[0].source == "auto" and characters[0].confidence == 0.6
    assert characters[0].first_seen_chapter == 2 and characters[0].last_seen_chapter == 2
    job = await _job(session_factory, job_id)
    assert job.status == "done" and job.attempt == 1


@pytest.mark.asyncio
async def test_already_summarized_version_is_skipped(session_factory, monkeypatch):
    job_id = await _seed(session_factory, version_summary="已有摘要")
    provider = StubProvider(text=GOOD_JSON)
    _patch_provider(monkeypatch, provider)

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "skipped"}
    assert provider.requests == []  # no model call
    async with session_factory() as session:
        version = await session.get(ChapterVersionModel, "v1")
        assert version.summary == "已有摘要"


@pytest.mark.asyncio
async def test_unparseable_output_becomes_summary_without_characters(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    provider = StubProvider(text="这不是 JSON，只是一段模型碎碎念。")
    _patch_provider(monkeypatch, provider)

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "characters": 0, "conflicts": 0}
    async with session_factory() as session:
        version = await session.get(ChapterVersionModel, "v1")
        assert version.summary == "这不是 JSON，只是一段模型碎碎念。"
        characters = list((await session.scalars(select(CharacterModel))).all())
    assert characters == []


@pytest.mark.asyncio
async def test_facts_produce_conflict_rows(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(CharacterModel(
            id="c-user", project_id="p1", name="李雷", aliases_json="[]", summary="", role="主角",
            status="active", source="user", confidence=1.0, created_at=now, updated_at=now,
        ))
        await session.commit()
    payload = json.dumps({
        "summary": "李雷黑化的前奏。",
        "characters": [],
        "facts": [{"entity": "李雷", "field": "角色", "value": "反派"}],
    }, ensure_ascii=False)
    _patch_provider(monkeypatch, StubProvider(text=payload))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "characters": 0, "conflicts": 1}
    async with session_factory() as session:
        rows = list((await session.scalars(select(CanonConflictModel))).all())
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert rows[0].candidate_source == "chapter_version:v1"
    assert rows[0].conflicting_source == "character:c-user"
    evidence = json.loads(rows[0].evidence_json)
    assert evidence["candidate_value"] == "反派" and evidence["existing_value"] == "主角"


@pytest.mark.asyncio
async def test_conflict_check_failure_does_not_break_job(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(text=GOOD_JSON))

    async def _boom(uow, **kwargs):
        raise RuntimeError("conflict check exploded")

    monkeypatch.setattr("proseforge.application.work.conflict_check.check_conflicts", _boom)

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "characters": 1, "conflicts": 0}
    job = await _job(session_factory, job_id)
    assert job.status == "done"


@pytest.mark.asyncio
async def test_missing_credential_fails_without_raising(session_factory, monkeypatch):
    job_id = await _seed(session_factory, with_credential=False)
    _patch_provider(monkeypatch, StubProvider(text=GOOD_JSON))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "failed"}
    job = await _job(session_factory, job_id)
    assert job.status == "failed" and job.error == "模型未配置"


@pytest.mark.asyncio
async def test_provider_error_rearms_pending_then_fails_at_max(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(error=RuntimeError("upstream down")))

    for expected_attempt in range(1, 3):
        with pytest.raises(RuntimeError):
            await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
        job = await _job(session_factory, job_id)
        assert job.status == "pending" and job.attempt == expected_attempt

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
    assert result == {"status": "failed"}
    job = await _job(session_factory, job_id)
    assert job.status == "failed" and job.attempt == 3


def test_parse_summary_payload_variants():
    summary, characters, facts, chapter_fact, character_states = parse_summary_payload('前置废话 {"summary": "摘要", "characters": [], "facts": [{"entity": "李雷", "field": "角色", "value": "主角"}]} 后置废话')
    assert summary == "摘要"
    assert characters == []
    assert facts == [{"entity": "李雷", "field": "角色", "value": "主角"}]
    assert chapter_fact == {} and character_states == []
    summary, characters, *_ = parse_summary_payload('{"summary": "s", "characters": [{"name": ""}, {"name": "甲", "aliases": "not-a-list"}, "junk"]}')
    assert [c["name"] for c in characters] == ["甲"]
    assert characters[0]["aliases"] == []
    summary, characters, facts, chapter_fact, character_states = parse_summary_payload("完全不是 JSON")
    assert summary == "完全不是 JSON"
    assert characters == [] and facts == [] and chapter_fact == {} and character_states == []
    # Character cap: 10; fact cap: 15; fact value clipped to 50 chars.
    payload = json.dumps({"summary": "s", "characters": [{"name": f"角色{i}"} for i in range(15)],
                          "facts": [{"entity": "甲", "field": "角色", "value": "长" * 80} for _ in range(20)]})
    _, characters, facts, _, _ = parse_summary_payload(payload)
    assert len(characters) == 10
    assert len(facts) == 15 and len(facts[0]["value"]) == 50
    # Missing facts key: fine, defaults to empty.
    _, _, facts, _, _ = parse_summary_payload('{"summary": "s"}')
    assert facts == []


def test_parse_summary_payload_state_sections():
    payload = json.dumps({
        "summary": "s",
        "chapter_fact": {
            "timeline": "当夜",
            "locations": {"李雷": "山谷", "烛龙": ""},
            "items": {"戒指": "李雷"},
            "revealed": ["师父的身份", ""],
            "time_anchor": "三日后",
            "junk": [1, 2],
        },
        "character_states": [
            {"name": "李雷", "emotion": "焦虑", "mental": "失眠", "note": "得知真相"},
            {"name": ""},
            {"name": "烛龙", "emotion": 1},
            "junk",
        ],
    }, ensure_ascii=False)
    _, _, _, chapter_fact, character_states = parse_summary_payload(payload)
    assert chapter_fact == {
        "timeline": "当夜",
        "locations": {"李雷": "山谷"},
        "items": {"戒指": "李雷"},
        "revealed": ["师父的身份"],
        "time_anchor": "三日后",
    }
    assert character_states == [
        {"name": "李雷", "emotion": "焦虑", "mental": "失眠", "note": "得知真相"},
        {"name": "烛龙", "emotion": "1", "mental": "", "note": ""},
    ]
    # Broken state sections alone never cost summary/characters.
    _, characters, _, chapter_fact, character_states = parse_summary_payload(
        '{"summary": "s", "characters": [{"name": "甲"}], "chapter_fact": "oops", "character_states": "oops"}'
    )
    assert [c["name"] for c in characters] == ["甲"]
    assert chapter_fact == {} and character_states == []


STATE_JSON = json.dumps({
    "summary": "李雷在山谷得到戒指。",
    "characters": [],
    "chapter_fact": {
        "timeline": "当夜",
        "locations": {"李雷": "山谷"},
        "items": {"戒指": "李雷"},
        "revealed": ["师父的身份"],
        "time_anchor": "三日后",
    },
    "character_states": [{"name": "李雷", "emotion": "焦虑", "mental": "失眠", "note": "刚得知真相"}],
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_state_entries_written_and_upserted(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(text=STATE_JSON))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["status"] == "done"
    async with session_factory() as session:
        rows = list((await session.scalars(select(StoryBibleEntryModel))).all())
    by_kind = {row.kind: row for row in rows}
    chapter_fact_row = by_kind["chapter_fact"]
    assert chapter_fact_row.key == "ch2" and chapter_fact_row.status == "active"
    assert chapter_fact_row.source == "auto" and chapter_fact_row.version == 1
    chapter_fact_value = json.loads(chapter_fact_row.value_json)
    assert chapter_fact_value["timeline"] == "当夜"
    assert chapter_fact_value["locations"] == {"李雷": "山谷"}
    assert chapter_fact_value["items"] == {"戒指": "李雷"}
    assert chapter_fact_value["revealed"] == ["师父的身份"]
    assert chapter_fact_value["time_anchor"] == "三日后"
    assert chapter_fact_value["chapter_no"] == 2
    state_row = by_kind["character_state"]
    assert state_row.key == "李雷" and state_row.version == 1
    state_value = json.loads(state_row.value_json)
    assert {key: state_value[key] for key in ("emotion", "mental", "note", "chapter_no")} == {
        "emotion": "焦虑", "mental": "失眠", "note": "刚得知真相", "chapter_no": 2,
    }

    # A later extraction for the same keys upserts in place: no duplicate
    # rows, version bumps, newer values merge over older ones.
    from proseforge.application.work.summarize_chapter import _persist_state_entries
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await _persist_state_entries(
            uow, project_id="p1", chapter_no=2,
            chapter_fact={"timeline": "次日"},
            character_states=[{"name": "李雷", "emotion": "平静", "mental": "正常", "note": "想通了"}],
        )
        await uow.commit()
    async with session_factory() as session:
        rows = list((await session.scalars(select(StoryBibleEntryModel))).all())
    assert len(rows) == 2
    by_kind = {row.kind: row for row in rows}
    chapter_fact_value = json.loads(by_kind["chapter_fact"].value_json)
    assert chapter_fact_value["timeline"] == "次日"  # overwritten
    assert chapter_fact_value["items"] == {"戒指": "李雷"}  # older field kept
    assert by_kind["chapter_fact"].version == 2
    state_value = json.loads(by_kind["character_state"].value_json)
    assert state_value["emotion"] == "平静" and state_value["chapter_no"] == 2
    assert by_kind["character_state"].version == 2


@pytest.mark.asyncio
async def test_missing_state_sections_write_nothing(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(text=GOOD_JSON))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["status"] == "done"
    async with session_factory() as session:
        rows = list((await session.scalars(select(StoryBibleEntryModel))).all())
    assert rows == []


@pytest.mark.asyncio
async def test_pipeline_memories_auto_accepted_and_sliced(session_factory, monkeypatch):
    """记忆优先写入侧：章节摘要落库时显著事实沉淀为自动激活的项目级记忆，
    任意后续 run 的记忆切片都能查到（先查再想的供数据源）。"""
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(text=STATE_JSON))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["status"] == "done"
    async with session_factory() as session:
        rows = list((await session.scalars(select(AgentMemoryModel))).all())
    by_key = {row.memory_key: row for row in rows}
    assert set(by_key) == {"人物状态·李雷", "时间线锚点", "关键道具·戒指", "所在地点·李雷", "重大揭示·ch2"}
    for row in rows:
        assert row.status == "ACCEPTED"  # 管线事实直接激活，不走人工审批
        assert row.run_id == ""  # PROJECT_WIDE_RUN：项目级，跨 run 可见
        assert row.source_artifact_id == "ch1"  # 章节 id 记账可回查
    from proseforge.application.agents.memory_service import (
        decode_value,
        load_memory_slice,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    assert decode_value(by_key["人物状态·李雷"])["value"] == "第2章：情绪：焦虑；精神：失眠；刚得知真相"
    assert decode_value(by_key["时间线锚点"])["value"] == "第2章：三日后"
    assert decode_value(by_key["重大揭示·ch2"])["value"] == "师父的身份"

    slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(session_factory), {"id": "any-later-run", "project_id": "p1"})
    assert {item["fact_key"] for item in slice_} == set(by_key)


@pytest.mark.asyncio
async def test_missing_state_sections_propose_no_memories(session_factory, monkeypatch):
    job_id = await _seed(session_factory)
    _patch_provider(monkeypatch, StubProvider(text=GOOD_JSON))

    result = await execute_summarize_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["status"] == "done"
    async with session_factory() as session:
        rows = list((await session.scalars(select(AgentMemoryModel))).all())
    assert rows == []
