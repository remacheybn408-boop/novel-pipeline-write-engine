"""RAG indexing pipeline regression tests (incident follow-up).

Production incident: 23 chapters were indexed into an EMPTY index and nobody
noticed. These tests pin the three guarantees that would have caught it:

1. end-to-end happy path persists chunks carrying the exact model identity;
2. a chapter whose content yields zero chunks fails the job VISIBLY
   (status=failed with a readable error) instead of being marked done;
3. a vector whose width disagrees with the model registry is rejected
   BEFORE any chunk row is written, with the dimension numbers in the error.

Runs execute_index_job against native sqlite (FK pragma on); the embedder is
a fake returning registry-aligned vectors — no model download, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
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
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)
from proseforge.infrastructure.embeddings.client import EmbeddingResult
from proseforge.infrastructure.embeddings.llama_server import (
    LLAMA_MODELS,
    LlamaServerEmbedder,
)
from tests.conftest import make_fk_engine

MASTER_KEY = "test-master-key-not-a-credential"  # local engine never decrypts

_LOCAL_MODEL = "BAAI/bge-m3"  # llama.cpp backend; registry dimension 1024
_REGISTRY_DIM = int(LLAMA_MODELS[_LOCAL_MODEL]["dimension"])
_EXPECTED_IDENTITY = f"local/{_LOCAL_MODEL}"

_TABLES = [
    UserModel.__table__,
    ProjectModel.__table__,
    ChapterModel.__table__,
    ChapterVersionModel.__table__,
    UserPreferenceModel.__table__,
    RetrievalJobModel.__table__,
    RetrievalDocumentModel.__table__,
    RetrievalChunkModel.__table__,
]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(
    session_factory,
    *,
    version_content: str,
    job_attempt: int = 0,
) -> str:
    """Seed user/project/chapter/active version + local-llama preference; returns job_id."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(UserModel(id="user-1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="proj-1", owner_id="user-1", slug="novel", title="Novel"))
        await session.flush()  # no ORM relationships -> parents must land first
        session.add(ChapterModel(id="chap-1", project_id="proj-1", chapter_no=1, title="第一章", status="DONE", active_version_id="ver-1"))
        session.add(ChapterVersionModel(id="ver-1", chapter_id="chap-1", version_no=1, content=version_content, content_hash="h1", word_count=len(version_content)))
        session.add(UserPreferenceModel(
            id="pref-1", user_id="user-1", key="embedding",
            value_json=json.dumps({"engine": "local", "local_model": _LOCAL_MODEL}), updated_at=now,
        ))
        session.add(RetrievalJobModel(
            id="job-1", project_id="proj-1", job_type="index_chapter", source_type="chapter",
            source_id="chap-1", status="pending", attempt=job_attempt, requested_at=now,
        ))
        await session.commit()
        return "job-1"


def _fake_llama_embedder(monkeypatch, *, dimension: int = _REGISTRY_DIM) -> list[list[str]]:
    """Replace server startup + embedding with a fake; returns the embed call log."""
    calls: list[list[str]] = []

    async def _ensure_ready(self):
        return None

    async def _embed(self, texts):
        calls.append(list(texts))
        return EmbeddingResult(vectors=[[0.5] * dimension for _ in texts], total_tokens=len(texts))

    monkeypatch.setattr(LlamaServerEmbedder, "ensure_ready", _ensure_ready)
    monkeypatch.setattr(LlamaServerEmbedder, "embed", _embed)
    return calls


async def _job(session_factory, job_id: str) -> RetrievalJobModel:
    async with session_factory() as session:
        return await session.get(RetrievalJobModel, job_id)


async def _chunks(session_factory) -> list[RetrievalChunkModel]:
    async with session_factory() as session:
        return list((await session.scalars(select(RetrievalChunkModel))).all())


@pytest.mark.asyncio
async def test_end_to_end_persists_chunks_with_registry_model_identity(session_factory, monkeypatch):
    """Happy path: index_chapter job runs to done, chunks land in
    retrieval_chunks, every vector has the registry dimension and the chunk
    rows carry the exact embedding-model identity of the chosen model."""
    calls = _fake_llama_embedder(monkeypatch)
    job_id = await _seed(
        session_factory,
        version_content="第一章正文，主角登场。\n\n第二段，冲突出现。\n\n第三段，悬念留下。",
    )

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "done"
    job = await _job(session_factory, job_id)
    assert job.status == "done" and job.error is None
    assert calls, "embedder was never invoked — chunks cannot exist"
    chunks = await _chunks(session_factory)
    assert len(chunks) > 0, "the incident signature: job done but retrieval_chunks empty"
    for chunk in chunks:
        assert chunk.status == "active"
        assert chunk.embedding is not None and len(chunk.embedding) == _REGISTRY_DIM
        assert chunk.embedding_model == _EXPECTED_IDENTITY
        assert chunk.embedding_version == "v1"
    # The document row ties chunks to the indexed source version.
    async with session_factory() as session:
        document = (await session.scalars(select(RetrievalDocumentModel))).one()
        assert document.source_version == "ver-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank_content", ["", "   \n\n   \t  "])
async def test_blank_content_fails_job_visibly_not_done(session_factory, monkeypatch, blank_content):
    """Empty/whitespace-only content chunks to nothing — that MUST surface as
    a failed job with a readable error. A 'done' here is exactly how the
    incident's 23 chapters silently vanished from the index."""
    calls = _fake_llama_embedder(monkeypatch)
    job_id = await _seed(session_factory, version_content=blank_content)

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "failed"
    job = await _job(session_factory, job_id)
    assert job.status == "failed"
    assert job.error and "0 chunks" in job.error  # readable, points at the chunker stage
    assert calls == [], "embedder must not run when there is nothing to embed"
    assert await _chunks(session_factory) == []


@pytest.mark.asyncio
async def test_off_dimension_vectors_are_rejected_before_any_write(session_factory, monkeypatch):
    """The embedder returning vectors whose width disagrees with the model
    registry must be caught by the dimension guard BEFORE upsert: the job
    fails with the dimension numbers in the error and retrieval_chunks stays
    empty. Seeded at the final attempt so the first failure is terminal."""
    wrong_dimension = _REGISTRY_DIM // 2
    calls = _fake_llama_embedder(monkeypatch, dimension=wrong_dimension)
    job_id = await _seed(
        session_factory,
        version_content="第一章正文，维度错误的嵌入向量不得落库。",
        job_attempt=MAX_ATTEMPTS - 1,
    )

    result = await execute_index_job({"job_id": job_id, "user_id": "user-1"}, session_factory, master_key=MASTER_KEY)

    assert result == "failed"
    assert calls, "embedder ran — the failure must come from the dimension guard"
    job = await _job(session_factory, job_id)
    assert job.status == "failed"
    assert job.error and "dimension mismatch" in job.error
    assert str(wrong_dimension) in job.error and str(_REGISTRY_DIM) in job.error
    assert await _chunks(session_factory) == []
