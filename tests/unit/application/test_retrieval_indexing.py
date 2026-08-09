"""Retrieval indexing worker: job state machine + idempotent upsert.

Runs execute_index_job against native sqlite (FK pragma on) with a real
encrypted credential/preference chain; only EmbeddingClient.embed is
patched (deterministic vectors, no network).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.indexing import MAX_ATTEMPTS, execute_index_job
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import ProviderCredentialModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)
from proseforge.infrastructure.embeddings.client import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
)
from proseforge.infrastructure.embeddings.local import LocalEmbedder
from proseforge.infrastructure.security.credential_cipher import (
    CredentialCipher,
    derive_key,
)
from tests.conftest import make_fk_engine

MASTER_KEY = base64.b64encode(b"k" * 32).decode()

_TABLES = [
    UserModel.__table__,
    ProjectModel.__table__,
    ChapterModel.__table__,
    ChapterVersionModel.__table__,
    ProviderCredentialModel.__table__,
    UserPreferenceModel.__table__,
    RetrievalJobModel.__table__,
    RetrievalDocumentModel.__table__,
    RetrievalChunkModel.__table__,
    RecapRollupModel.__table__,
]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _encrypted_credential(user_id: str, provider: str, cred_id: str) -> str:
    payload = json.dumps({"api_key": "sk-test", "base_url": "https://api.example.com/v1"}).encode()
    encrypted = CredentialCipher(derive_key(MASTER_KEY)).encrypt(
        payload, associated_data=f"{user_id}:{provider}:{cred_id}".encode()
    )
    return base64.b64encode(encrypted).decode()


async def _seed(
    session_factory,
    *,
    with_config: bool = True,
    with_credential: bool = True,
    preference: dict | None = None,
    version_content: str = "第一章正文。\n\n第二段。",
) -> str:
    """Seed user/project/chapter/active version + embedding config; returns job_id."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="user-1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="proj-1", owner_id="user-1", slug="novel", title="Novel"))
        await session.flush()  # no ORM relationships -> parents must land first
        session.add(ChapterModel(id="chap-1", project_id="proj-1", chapter_no=1, title="第一章", status="DONE", active_version_id="ver-1"))
        session.add(ChapterVersionModel(id="ver-1", chapter_id="chap-1", version_no=1, content=version_content, content_hash="h1", word_count=len(version_content)))
        if with_config:
            if with_credential:
                session.add(ProviderCredentialModel(id="cred-1", user_id="user-1", provider="openai", encrypted_payload=_encrypted_credential("user-1", "openai", "cred-1")))
            session.add(UserPreferenceModel(id="pref-1", user_id="user-1", key="embedding", value_json=json.dumps(preference or {"provider": "openai", "model": "embed-1"}), updated_at=now))
        job = RetrievalJobModel(
            id="job-1", project_id="proj-1", job_type="index_chapter", source_type="chapter",
            source_id="chap-1", status="pending", attempt=0, requested_at=now,
        )
        session.add(job)
        await session.commit()
        return job.id


def _fake_embed(monkeypatch, *, fail: Exception | None = None) -> None:
    async def _embed(self, texts):
        if fail is not None:
            raise fail
        return EmbeddingResult(vectors=[[float(i), 0.5] for i, _ in enumerate(texts)], total_tokens=sum(len(t) for t in texts))

    monkeypatch.setattr(EmbeddingClient, "embed", _embed)


async def _job(session_factory, job_id: str) -> RetrievalJobModel:
    async with session_factory() as session:
        return await session.get(RetrievalJobModel, job_id)


async def _chunks(session_factory, *, status: str | None = None) -> list[RetrievalChunkModel]:
    from sqlalchemy import select

    async with session_factory() as session:
        statement = select(RetrievalChunkModel)
        if status is not None:
            statement = statement.where(RetrievalChunkModel.status == status)
        return list((await session.scalars(statement)).all())


@pytest.mark.asyncio
async def test_happy_path_indexes_active_version(session_factory, monkeypatch):
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory)

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "done"
    job = await _job(session_factory, job_id)
    assert job.status == "done" and job.attempt == 1 and job.error is None
    chunks = await _chunks(session_factory, status="active")
    assert len(chunks) == 1  # short chapter -> single chunk
    assert chunks[0].embedding == [0.0, 0.5]
    assert chunks[0].embedding_model == "openai/embed-1"
    assert chunks[0].embedding_version == "v1"
    assert len(chunks[0].content_hash) == 64


@pytest.mark.asyncio
async def test_same_version_rerun_writes_nothing(session_factory, monkeypatch):
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory)
    assert await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY) == "done"
    before = await _chunks(session_factory)

    # A second job for the same source hits the same source_version -> skipped.
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(RetrievalJobModel(
            id="job-2", project_id="proj-1", job_type="index_chapter", source_type="chapter",
            source_id="chap-1", status="pending", attempt=0, requested_at=now,
        ))
        await session.commit()
    result = await execute_index_job({"job_id": "job-2", "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "skipped"
    after = await _chunks(session_factory)
    assert [c.id for c in after] == [c.id for c in before]
    assert all(c.status == "active" for c in after)


@pytest.mark.asyncio
async def test_new_version_supersedes_old_chunks(session_factory, monkeypatch):
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory)
    assert await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY) == "done"

    now = datetime.now(UTC)
    async with session_factory() as session:
        chapter = await session.get(ChapterModel, "chap-1")
        session.add(ChapterVersionModel(id="ver-2", chapter_id="chap-1", version_no=2, content="修订后的第一章。\n\n全新段落。", content_hash="h2", word_count=20))
        chapter.active_version_id = "ver-2"
        session.add(RetrievalJobModel(
            id="job-3", project_id="proj-1", job_type="index_chapter", source_type="chapter",
            source_id="chap-1", status="pending", attempt=0, requested_at=now,
        ))
        await session.commit()

    assert await execute_index_job({"job_id": "job-3", "user_id": "user-1"}, session_factory, master_key=MASTER_KEY) == "done"

    active = await _chunks(session_factory, status="active")
    superseded = await _chunks(session_factory, status="superseded")
    assert len(active) == 1 and active[0].content == "修订后的第一章。\n全新段落。"
    assert len(superseded) == 1
    async with session_factory() as session:
        from sqlalchemy import select

        document = (await session.scalars(select(RetrievalDocumentModel))).one()
        assert document.source_version == "ver-2"


@pytest.mark.asyncio
async def test_missing_embedding_config_fails_without_retry(session_factory, monkeypatch):
    _fake_embed(monkeypatch)
    # engine=api configured but no stored credential -> terminal, never retried.
    job_id = await _seed(
        session_factory,
        with_credential=False,
        preference={"engine": "api", "provider": "openai", "model": "embed-1"},
    )

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.status == "failed"
    assert job.error == "embedding 未配置"
    assert await _chunks(session_factory) == []


@pytest.mark.asyncio
async def test_engine_off_indexes_chunks_without_vectors(session_factory, monkeypatch):
    _fake_embed(monkeypatch)  # must not be called: engine "off" never embeds
    job_id = await _seed(
        session_factory, with_credential=False, preference={"engine": "off"}
    )

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "done"
    job = await _job(session_factory, job_id)
    assert job.status == "done" and job.error is None
    chunks = await _chunks(session_factory, status="active")
    assert len(chunks) == 1
    assert chunks[0].embedding is None
    assert chunks[0].embedding_model == "none"
    assert chunks[0].embedding_version == "v1"


@pytest.mark.asyncio
async def test_engine_local_indexes_with_identity_and_tighter_window(session_factory, monkeypatch):
    async def _ensure_ready(self):
        self._text_embedding = object()

    async def _embed(self, texts):
        # 512 dims: matches the bge-small-zh registry entry (the worker
        # asserts vector width against the registered dimension).
        return EmbeddingResult(vectors=[[0.5] * 512 for _ in texts], total_tokens=len(texts))

    monkeypatch.setattr(LocalEmbedder, "ensure_ready", _ensure_ready)
    monkeypatch.setattr(LocalEmbedder, "embed", _embed)
    job_id = await _seed(
        session_factory,
        with_credential=False,
        preference={"engine": "local", "local_model": "BAAI/bge-small-zh-v1.5"},
        version_content="字" * 1000,  # single paragraph -> hard-split at the local window
    )

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "done"
    chunks = await _chunks(session_factory, status="active")
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk.content) <= 450
        assert chunk.embedding_model == "local/BAAI/bge-small-zh-v1.5"
        assert chunk.embedding is not None


@pytest.mark.asyncio
async def test_transient_error_rearms_pending_then_fails_at_max_attempts(session_factory, monkeypatch):
    _fake_embed(monkeypatch, fail=EmbeddingError("upstream down"))
    job_id = await _seed(session_factory)

    for expected_attempt in range(1, MAX_ATTEMPTS):
        with pytest.raises(EmbeddingError):
            await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)
        job = await _job(session_factory, job_id)
        assert job.status == "pending"
        assert job.attempt == expected_attempt
        assert "EmbeddingError" in job.error

    # Attempt MAX_ATTEMPTS: terminal failure, no raise.
    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)
    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.status == "failed" and job.attempt == MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_non_pending_job_is_not_reclaimed(session_factory, monkeypatch):
    """Atomic claim: only a pending row can transition to running. A
    duplicate dispatch (sweeper replay / lease-expiry redelivery) against a
    running/done/failed job skips without touching it."""
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory)

    for status in ("running", "done", "failed"):
        async with session_factory() as session:
            job = await session.get(RetrievalJobModel, job_id)
            job.status = status
            await session.commit()

        result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

        assert result == "skipped"
        job = await _job(session_factory, job_id)
        assert job.status == status and job.attempt == 0
    assert await _chunks(session_factory) == []


@pytest.mark.asyncio
async def test_missing_source_fails_job(session_factory, monkeypatch):
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory)
    async with session_factory() as session:
        job = await session.get(RetrievalJobModel, job_id)
        job.source_id = "chap-missing"
        await session.commit()

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.error == "source not found"


@pytest.mark.asyncio
async def test_empty_chunker_output_marks_failed_not_done(session_factory, monkeypatch):
    """Whitespace-only content chunks to nothing — that is always a bug, and
    a "done" here would leave the chapter silently unindexed forever."""
    _fake_embed(monkeypatch)
    job_id = await _seed(session_factory, version_content="   \n\n   ")

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.status == "failed"
    assert "0 chunks" in (job.error or "")
    assert await _chunks(session_factory) == []


@pytest.mark.asyncio
async def test_dimension_mismatch_fails_loudly(session_factory, monkeypatch):
    """A vector whose width disagrees with the embedder's registered
    dimension must fail the job, not poison the index."""
    from proseforge.infrastructure.embeddings.local import LocalEmbedder

    async def _embed_wrong_dim(self, texts):
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in texts], total_tokens=1)

    async def _noop_ready(self):
        return None

    monkeypatch.setattr(LocalEmbedder, "ensure_ready", _noop_ready)
    monkeypatch.setattr(LocalEmbedder, "embed", _embed_wrong_dim)
    job_id = await _seed(
        session_factory,
        with_credential=False,
        preference={"engine": "local", "local_model": "BAAI/bge-small-zh-v1.5"},
    )

    for _ in range(1, MAX_ATTEMPTS):
        with pytest.raises(EmbeddingError, match="dimension mismatch"):
            await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)
    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.status == "failed"
    assert "dimension mismatch" in (job.error or "")
    assert await _chunks(session_factory) == []


# -- recap-rollup indexing (phase-2 item 9) -------------------------------------


async def _seed_recap(session_factory, *, stale: bool = False, content: str = "卷一梗概：主线推进，伏笔未结。") -> str:
    """Seed user/project/embedding config + one recap row + its index_recap
    job; returns job_id."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="user-1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="proj-1", owner_id="user-1", slug="novel", title="Novel"))
        await session.flush()
        session.add(ProviderCredentialModel(id="cred-1", user_id="user-1", provider="openai", encrypted_payload=_encrypted_credential("user-1", "openai", "cred-1")))
        session.add(UserPreferenceModel(id="pref-1", user_id="user-1", key="embedding", value_json=json.dumps({"provider": "openai", "model": "embed-1"}), updated_at=now))
        session.add(RecapRollupModel(
            id="roll-1", project_id="proj-1", user_id="user-1", level="volume",
            span_start=1, span_end=6, content=content, source_version_ids="[]",
            stale=stale, created_at=now, updated_at=now,
        ))
        session.add(RetrievalJobModel(
            id="job-r1", project_id="proj-1", job_type="index_recap", source_type="recap_rollup",
            source_id="roll-1", status="pending", attempt=0, requested_at=now,
        ))
        await session.commit()
    return "job-r1"


@pytest.mark.asyncio
async def test_settled_recap_indexed_as_derived(session_factory, monkeypatch):
    """定稿（非 stale）卷梗概走同一 indexing 通道入 RAG：authority_level=
    derived，标题/章节跨度/metadata 按梗概标注。"""
    _fake_embed(monkeypatch)
    job_id = await _seed_recap(session_factory)

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "done"
    async with session_factory() as session:
        from sqlalchemy import select

        document = (await session.scalars(select(RetrievalDocumentModel))).one()
        assert document.source_type == "recap_rollup" and document.source_id == "roll-1"
        assert document.authority_level == "derived"
        assert document.title == "卷梗概（第1-6章）"
        assert (document.chapter_from, document.chapter_to) == (1, 6)
        assert document.status == "active"
    chunks = await _chunks(session_factory, status="active")
    assert len(chunks) == 1
    metadata = json.loads(chunks[0].metadata_json)
    assert metadata == {"recap_level": "volume", "span_start": 1, "span_end": 6}
    assert "chapter_no" not in metadata

    # Same content re-run: source_version is the content hash -> zero writes.
    async with session_factory() as session:
        session.add(RetrievalJobModel(
            id="job-r2", project_id="proj-1", job_type="index_recap", source_type="recap_rollup",
            source_id="roll-1", status="pending", attempt=0, requested_at=datetime.now(UTC),
        ))
        await session.commit()
    assert await execute_index_job({"job_id": "job-r2", "user_id": "user-1"}, session_factory, master_key=MASTER_KEY) == "skipped"


@pytest.mark.asyncio
async def test_stale_recap_is_never_indexed(session_factory, monkeypatch):
    """stale 梗概不入索引（失效标记已 supersede 旧块，迟到的索引任务直接
    done，零写入）——堵过期梗概外泄为检索证据。"""
    _fake_embed(monkeypatch)
    job_id = await _seed_recap(session_factory, stale=True)

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "skipped"
    job = await _job(session_factory, job_id)
    assert job.status == "done" and job.error is None
    assert await _chunks(session_factory) == []
    async with session_factory() as session:
        from sqlalchemy import select

        assert list(await session.scalars(select(RetrievalDocumentModel))) == []
