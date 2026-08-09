"""rollup_recap worker: volume-boundary parsing, volume/book/era rollup
production with a stubbed provider, incremental book recaps, the
10-volume era roll, and the never-write-empty failure discipline. Also
covers the summarize_chapter trigger (volume-end enqueue + dispatch) and
the phase-2 item-12 mandatory summary prompt elements.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.work.rollup_recap import (
    execute_rollup_job,
    fallback_volume_span,
    parse_volume_spans,
    volume_end_span,
    volume_index,
)
from proseforge.application.work.summarize_chapter import (
    _USER_PROMPT_TEMPLATE,
    execute_summarize_job,
    parse_summary_payload,
)
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import (
    AuditLogModel,
    ProviderCredentialModel,
)
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
    RecapRollupModel.__table__, AgentRunModel.__table__, AgentEventModel.__table__, AuditLogModel.__table__,
]


class StubProvider:
    provider_id = "openai"

    def __init__(self, texts: list[str] | None = None, error: Exception | None = None):
        self.texts = list(texts or [])
        self.error = error
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        yield GenerationEvent("content.delta", self.texts.pop(0) if self.texts else "")


class StubQueue:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, name: str, payload: dict[str, object]) -> None:
        self.calls.append((name, payload))


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _credential_payload(user_id: str, provider: str, cred_id: str) -> str:
    encrypted = CredentialCipher(derive_key(MASTER_KEY)).encrypt(
        json.dumps({"api_key": "sk-test"}).encode(), associated_data=f"{user_id}:{provider}:{cred_id}".encode()
    )
    return base64.b64encode(encrypted).decode()


async def _seed_base(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        await session.flush()
        project = await session.get(ProjectModel, "p1")
        project.writing_model_provider = "openai"
        project.writing_model_id = "gpt-lock"
        project.model_locked_at = now
        project.model_lock_source = "first_chapter"
        session.add(ProviderCredentialModel(
            id="cred-1", user_id="u1", provider="openai",
            encrypted_payload=_credential_payload("u1", "openai", "cred-1"),
        ))
        await session.commit()


async def _add_chapters(session_factory, numbers: list[int], *, with_summary: bool = True) -> None:
    """Chapters 1..N style rows: chapter id chN, active version vN."""
    async with session_factory() as session:
        for number in numbers:
            session.add(ChapterModel(
                id=f"ch{number}", project_id="p1", chapter_no=number,
                title=f"第{number}章", status="DONE", active_version_id=f"v{number}",
            ))
            session.add(ChapterVersionModel(
                id=f"v{number}", chapter_id=f"ch{number}", version_no=1,
                content=f"第{number}章正文", content_hash=f"h{number}", word_count=6,
                summary=f"第{number}章摘要：主线推进。" if with_summary else "",
            ))
        await session.commit()


async def _add_batch_plan(session_factory, goal: str) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(AgentRunModel(
            id="ar1", user_id="u1", project_id="p1", goal_hash="gh", goal=goal,
            graph_revision=1, status="COMPLETED", created_at=now, updated_at=now,
        ))
        await session.flush()  # parent row first: no ORM relationships, so FK order is not auto-sorted
        session.add(AgentEventModel(id="ae1", run_id="ar1", sequence=1, event_type="batch.planned", payload="{}"))
        await session.commit()


async def _add_rollup_job(session_factory, chapter_id: str, job_id: str = "rjob-1") -> str:
    async with session_factory() as session:
        session.add(RetrievalJobModel(
            id=job_id, project_id="p1", job_type="rollup_recap", source_type="chapter",
            source_id=chapter_id, status="pending", attempt=0, requested_at=datetime.now(UTC),
        ))
        await session.commit()
    return job_id


async def _rollups(session_factory) -> list[RecapRollupModel]:
    async with session_factory() as session:
        return list((await session.scalars(select(RecapRollupModel).order_by(RecapRollupModel.level, RecapRollupModel.span_start))).all())


async def _job(session_factory, job_id: str) -> RetrievalJobModel:
    async with session_factory() as session:
        return await session.get(RetrievalJobModel, job_id)


def _patch_provider(monkeypatch, provider: StubProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


# -- volume boundary parsing -------------------------------------------------


def test_parse_volume_spans_from_outline_labels():
    text = "卷一（第 1-6 章：末世降临）\n一些正文\n卷二（第7到12章：反击）\n卷三（第 13—18 章：结局）"
    assert parse_volume_spans(text) == [(1, 6), (7, 12), (13, 18)]
    # Broken ranges and overlaps drop out, nothing crashes.
    assert parse_volume_spans("卷一（第 5-2 章：坏）卷二（第 3-8 章：好）") == [(3, 8)]
    assert parse_volume_spans("卷一（第 1-6 章）卷二（第 4-9 章：重叠）") == [(1, 6)]
    assert parse_volume_spans("") == [] and parse_volume_spans("没有卷标的大纲") == []


def test_volume_end_span_labeled_and_fallback():
    labeled = [(1, 6), (7, 12)]
    assert volume_end_span(6, labeled) == (1, 6)
    assert volume_end_span(12, labeled) == (7, 12)
    assert volume_end_span(5, labeled) is None
    assert volume_end_span(10, labeled) is None  # mid labeled volume
    # Beyond the labeled range the fixed 10-chapter fallback takes over.
    assert volume_end_span(20, labeled) == (11, 20)
    assert volume_end_span(13, labeled) is None
    # Pure fallback: ends land on 10/20/30.
    assert volume_end_span(10, []) == (1, 10)
    assert volume_end_span(20, []) == (11, 20)
    assert volume_end_span(9, []) is None
    assert fallback_volume_span(11) == (11, 20)
    assert volume_index((21, 30), []) == 3


# -- rollup production --------------------------------------------------------


@pytest.mark.asyncio
async def test_volume_and_book_seed_rollup(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3])
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）\n卷二（第 4-6 章：承）")
    job_id = await _add_rollup_job(session_factory, "ch3")
    provider = StubProvider(texts=["卷一梗概：主线/人物/伏笔/道具。", "全书梗概：以卷一为底。"])
    _patch_provider(monkeypatch, provider)

    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "level": "volume", "span": [1, 3], "era": False}
    rows = await _rollups(session_factory)
    by_level = {row.level: row for row in rows}
    volume, book = by_level["volume"], by_level["book"]
    assert (volume.span_start, volume.span_end) == (1, 3)
    assert volume.content == "卷一梗概：主线/人物/伏笔/道具。"
    assert json.loads(volume.source_version_ids) == ["v1", "v2", "v3"]
    assert volume.stale is False and volume.user_id == "u1"
    # Book recap seeded from the first volume: one book row, span 1..3.
    assert (book.span_start, book.span_end) == (1, 3)
    assert json.loads(book.source_version_ids) == [volume.id]
    # Two LLM calls: volume compression, then book seeding; prompts carry
    # the mandatory elements and the chapter summaries.
    assert len(provider.requests) == 2
    volume_prompt = provider.requests[0].input_blocks[0]["text"]
    assert "第1章摘要" in volume_prompt and "第3章摘要" in volume_prompt
    assert "主线进展" in volume_prompt and "未结伏笔" in volume_prompt
    book_prompt = provider.requests[1].input_blocks[0]["text"]
    assert "卷一梗概" in book_prompt
    job = await _job(session_factory, job_id)
    assert job.status == "done"
    async with session_factory() as session:
        audits = list((await session.scalars(select(AuditLogModel).where(AuditLogModel.action == "recap.rollup"))).all())
    assert len(audits) == 2
    assert {json.loads(row.payload)["level"] for row in audits} == {"volume", "book"}


@pytest.mark.asyncio
async def test_rollup_enqueues_recap_index_jobs(session_factory, monkeypatch):
    """定稿梗概入 RAG（第 9 项）：rollup 落库同一 commit 入队 index_recap
    任务（volume+book 各一），commit 后派发给 indexing worker。"""
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3])
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）")
    job_id = await _add_rollup_job(session_factory, "ch3")
    provider = StubProvider(texts=["卷一梗概：主线/人物/伏笔/道具。", "全书梗概：以卷一为底。"])
    _patch_provider(monkeypatch, provider)
    queue = StubQueue()

    result = await execute_rollup_job(
        {"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY, queue=queue
    )

    assert result["status"] == "done"
    recap_ids = {row.id for row in await _rollups(session_factory)}
    async with session_factory() as session:
        index_jobs = list((await session.scalars(
            select(RetrievalJobModel).where(RetrievalJobModel.job_type == "index_recap")
        )).all())
    assert len(index_jobs) == 2  # volume + book
    assert {job.source_type for job in index_jobs} == {"recap_rollup"}
    assert {job.source_id for job in index_jobs} == recap_ids
    assert {job.status for job in index_jobs} == {"pending"}
    assert {name for name, _payload in queue.calls} == {"proseforge.retrieval.index_document"}
    assert sorted(str(payload["job_id"]) for _name, payload in queue.calls) == sorted(job.id for job in index_jobs)


@pytest.mark.asyncio
async def test_book_rollup_is_incremental(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3, 4, 5, 6])
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）\n卷二（第 4-6 章：承）")
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(RecapRollupModel(
            id="book-0", project_id="p1", user_id="u1", level="book", span_start=1, span_end=3,
            content="旧全书梗概：卷一结束了。", source_version_ids='["old-vol"]',
            stale=True, created_at=now, updated_at=now,
        ))
        await session.commit()
    job_id = await _add_rollup_job(session_factory, "ch6")
    provider = StubProvider(texts=["卷二梗概。", "新全书梗概：旧+卷二。"])
    _patch_provider(monkeypatch, provider)

    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["status"] == "done"
    # Incremental: the book prompt carries BOTH the old book recap and the
    # new volume recap — no full recompute over chapter summaries.
    book_prompt = provider.requests[1].input_blocks[0]["text"]
    assert "旧全书梗概：卷一结束了。" in book_prompt
    assert "卷二梗概。" in book_prompt
    rows = await _rollups(session_factory)
    books = [row for row in rows if row.level == "book"]
    assert len(books) == 1
    assert books[0].id == "book-0"  # updated in place, not re-inserted
    assert books[0].content == "新全书梗概：旧+卷二。"
    assert books[0].span_end == 6 and books[0].stale is False
    volume = next(row for row in rows if row.level == "volume")
    assert json.loads(books[0].source_version_ids) == ["book-0", volume.id]


@pytest.mark.asyncio
async def test_era_rollup_every_ten_volumes(session_factory, monkeypatch):
    await _seed_base(session_factory)
    # Fallback boundaries (no batch plan): chapters 91-100 close volume 10.
    await _add_chapters(session_factory, list(range(91, 101)))
    now = datetime.now(UTC)
    async with session_factory() as session:
        for volume_no in range(1, 10):  # volumes 1..9 already recapped
            start = (volume_no - 1) * 10 + 1
            session.add(RecapRollupModel(
                id=f"vol-{volume_no}", project_id="p1", user_id="u1", level="volume",
                span_start=start, span_end=start + 9, content=f"第{volume_no}卷梗概。",
                source_version_ids="[]", stale=False, created_at=now, updated_at=now,
            ))
        await session.commit()
    job_id = await _add_rollup_job(session_factory, "ch100")
    provider = StubProvider(texts=["第10卷梗概。", "全书梗概：百章。", "纪元梗概：十卷合一。"])
    _patch_provider(monkeypatch, provider)

    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "done", "level": "volume", "span": [91, 100], "era": True}
    assert len(provider.requests) == 3  # volume + book + era
    era_prompt = provider.requests[2].input_blocks[0]["text"]
    assert "第1卷梗概。" in era_prompt and "第9卷梗概。" in era_prompt and "第10卷梗概。" in era_prompt
    rows = await _rollups(session_factory)
    era = next(row for row in rows if row.level == "era")
    assert (era.span_start, era.span_end) == (1, 100)
    assert era.content == "纪元梗概：十卷合一。"
    volume10 = next(row for row in rows if row.level == "volume" and row.span_start == 91)
    assert json.loads(era.source_version_ids) == [f"vol-{no}" for no in range(1, 10)] + [volume10.id]


@pytest.mark.asyncio
async def test_era_roll_skipped_without_prior_volume_recaps(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, list(range(91, 101)))
    job_id = await _add_rollup_job(session_factory, "ch100")
    provider = StubProvider(texts=["第10卷梗概。", "全书梗概。"])
    _patch_provider(monkeypatch, provider)

    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result["era"] is False
    assert len(provider.requests) == 2  # no era LLM call
    assert all(row.level != "era" for row in await _rollups(session_factory))


@pytest.mark.asyncio
async def test_mid_volume_chapter_skips(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3])
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）")
    job_id = await _add_rollup_job(session_factory, "ch2")  # not a volume end
    provider = StubProvider(texts=["不会被用到。"])
    _patch_provider(monkeypatch, provider)

    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)

    assert result == {"status": "skipped"}
    assert provider.requests == []
    assert await _rollups(session_factory) == []
    job = await _job(session_factory, job_id)
    assert job.status == "done"


@pytest.mark.asyncio
async def test_failure_never_writes_empty_content(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3])
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）")
    job_id = await _add_rollup_job(session_factory, "ch3")

    # Provider error: retry chain re-arms pending, nothing persisted.
    _patch_provider(monkeypatch, StubProvider(error=RuntimeError("upstream down")))
    with pytest.raises(RuntimeError):
        await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
    job = await _job(session_factory, job_id)
    assert job.status == "pending" and job.attempt == 1
    assert await _rollups(session_factory) == []

    # Empty model output is an error too — retried, never written.
    _patch_provider(monkeypatch, StubProvider(texts=["   "]))
    with pytest.raises(RuntimeError, match="empty recap"):
        await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
    job = await _job(session_factory, job_id)
    assert job.status == "pending" and job.attempt == 2
    assert await _rollups(session_factory) == []

    # Third strike: permanently failed, still zero rows.
    result = await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
    assert result == {"status": "failed"}
    job = await _job(session_factory, job_id)
    assert job.status == "failed" and job.attempt == 3
    assert await _rollups(session_factory) == []


@pytest.mark.asyncio
async def test_missing_summaries_retry_instead_of_empty_rollup(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [1, 2, 3], with_summary=False)
    await _add_batch_plan(session_factory, "卷一（第 1-3 章：起）")
    job_id = await _add_rollup_job(session_factory, "ch3")
    provider = StubProvider(texts=["不会被用到。"])
    _patch_provider(monkeypatch, provider)

    with pytest.raises(RuntimeError, match="no chapter summaries"):
        await execute_rollup_job({"job_id": job_id, "user_id": "u1"}, session_factory, master_key=MASTER_KEY)
    assert provider.requests == []
    assert await _rollups(session_factory) == []
    job = await _job(session_factory, job_id)
    assert job.status == "pending"


# -- summarize_chapter trigger (volume end -> enqueue + dispatch) -------------


@pytest.mark.asyncio
async def test_summarize_enqueues_rollup_on_volume_end(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [10], with_summary=False)  # fallback volume (1,10)
    async with session_factory() as session:
        session.add(RetrievalJobModel(
            id="job-1", project_id="p1", job_type="summarize_chapter", source_type="chapter_version",
            source_id="v10", status="pending", attempt=0, requested_at=datetime.now(UTC),
        ))
        await session.commit()
    summary_json = json.dumps({"summary": "第十章摘要。", "characters": []}, ensure_ascii=False)
    _patch_provider(monkeypatch, StubProvider(texts=[summary_json]))
    queue = StubQueue()

    result = await execute_summarize_job(
        {"job_id": "job-1", "user_id": "u1"}, session_factory, master_key=MASTER_KEY, queue=queue
    )

    assert result["status"] == "done"
    async with session_factory() as session:
        rollups = list((await session.scalars(
            select(RetrievalJobModel).where(RetrievalJobModel.job_type == "rollup_recap")
        )).all())
    assert len(rollups) == 1
    assert rollups[0].source_type == "chapter" and rollups[0].source_id == "ch10"
    assert rollups[0].status == "pending"
    assert queue.calls == [("proseforge.work.rollup_recap", {"job_id": rollups[0].id, "user_id": "u1"})]


@pytest.mark.asyncio
async def test_summarize_mid_volume_enqueues_nothing(session_factory, monkeypatch):
    await _seed_base(session_factory)
    await _add_chapters(session_factory, [2], with_summary=False)  # mid fallback volume
    async with session_factory() as session:
        session.add(RetrievalJobModel(
            id="job-1", project_id="p1", job_type="summarize_chapter", source_type="chapter_version",
            source_id="v2", status="pending", attempt=0, requested_at=datetime.now(UTC),
        ))
        await session.commit()
    summary_json = json.dumps({"summary": "第二章摘要。", "characters": []}, ensure_ascii=False)
    _patch_provider(monkeypatch, StubProvider(texts=[summary_json]))
    queue = StubQueue()

    result = await execute_summarize_job(
        {"job_id": "job-1", "user_id": "u1"}, session_factory, master_key=MASTER_KEY, queue=queue
    )

    assert result["status"] == "done"
    async with session_factory() as session:
        rollups = list((await session.scalars(
            select(RetrievalJobModel).where(RetrievalJobModel.job_type == "rollup_recap")
        )).all())
    assert rollups == []
    assert queue.calls == []


# -- item 12: mandatory chapter-summary prompt elements -----------------------


def test_summary_prompt_forces_pyramid_foundation_elements():
    assert "未结伏笔" in _USER_PROMPT_TEMPLATE
    assert "人物状态变化" in _USER_PROMPT_TEMPLATE
    assert "关键道具" in _USER_PROMPT_TEMPLATE
    assert "主线进展" in _USER_PROMPT_TEMPLATE
    assert "open_loops" in _USER_PROMPT_TEMPLATE


def test_parse_summary_payload_open_loops():
    payload = json.dumps({
        "summary": "s",
        "chapter_fact": {"open_loops": ["戒指的来历", "", "师父的身份"]},
    }, ensure_ascii=False)
    _, _, _, chapter_fact, _ = parse_summary_payload(payload)
    assert chapter_fact == {"open_loops": ["戒指的来历", "师父的身份"]}
    # Missing/broken open_loops never sinks the rest of chapter_fact.
    _, _, _, chapter_fact, _ = parse_summary_payload(
        '{"summary": "s", "chapter_fact": {"timeline": "当夜", "open_loops": "oops"}}'
    )
    assert chapter_fact == {"timeline": "当夜"}
