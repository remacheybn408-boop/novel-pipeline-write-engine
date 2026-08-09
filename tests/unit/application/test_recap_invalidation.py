"""Memory-pyramid invalidation (phase-2 item 8): same-commit stale marking,
summary re-enqueue, RAG supersede, stale filtering in scene_state, and the
lazy recompute enqueue.

The invalidation funnel is repositories/chapter.py set_active_version —
all seven production call sites (agent_executor, workflows/tasks,
application/workflows/executor, approve_proposal, importer, chapters API
x2) route through it (grep-verified). The funnel is exercised directly
via uow.chapters, plus one representative end-to-end call site
(approve_persisted_proposal) to prove the wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.revision.approve_proposal import approve_persisted_proposal
from proseforge.application.work.rollup_recap import enqueue_stale_recap_recompute
from proseforge.application.work.scene_state import build_scene_state
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.character import CharacterModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.recap import RecapRollupModel
from proseforge.infrastructure.database.models.remaining import AuditLogModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)
from proseforge.infrastructure.database.models.revision import RevisionProposalModel
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine

_TABLES = [
    UserModel.__table__,
    ProjectModel.__table__,
    ChapterModel.__table__,
    ChapterVersionModel.__table__,
    CharacterModel.__table__,
    StoryBibleEntryModel.__table__,
    RecapRollupModel.__table__,
    RetrievalJobModel.__table__,
    RetrievalDocumentModel.__table__,
    RetrievalChunkModel.__table__,
    AuditLogModel.__table__,
    RevisionProposalModel.__table__,
]


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(UserModel(id="u1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="p1", owner_id="u1", slug="novel", title="Novel"))
        await session.commit()
    yield factory
    await engine.dispose()


def _rollup(rollup_id: str, level: str, span_start: int, span_end: int, *, stale: bool = False) -> RecapRollupModel:
    now = datetime.now(UTC)
    return RecapRollupModel(
        id=rollup_id, project_id="p1", user_id="u1", level=level,
        span_start=span_start, span_end=span_end, content=f"{level}梗概 {span_start}-{span_end}",
        source_version_ids="[]", stale=stale, created_at=now, updated_at=now,
    )


async def _seed_chapters(session_factory, numbers: list[int]) -> None:
    async with session_factory() as session:
        for number in numbers:
            session.add(ChapterModel(
                id=f"ch{number}", project_id="p1", chapter_no=number,
                title=f"第{number}章", status="DONE", active_version_id=f"v{number}",
            ))
            session.add(ChapterVersionModel(
                id=f"v{number}", chapter_id=f"ch{number}", version_no=1,
                content=f"第{number}章正文", content_hash=f"h{number}", word_count=6,
                summary=f"第{number}章摘要",
            ))
        await session.commit()


async def _seed_rollups_with_index(session_factory) -> None:
    """Volume(1-3) + book(1-3) + era(1-30) recaps, a non-covering volume(4-6),
    and an indexed RAG document for the volume(1-3) recap."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(_rollup("roll-v1", "volume", 1, 3))
        session.add(_rollup("roll-book", "book", 1, 3))
        session.add(_rollup("roll-era", "era", 1, 30))
        session.add(_rollup("roll-v2", "volume", 4, 6))
        session.add(RetrievalDocumentModel(
            id="doc-v1", project_id="p1", source_type="recap_rollup", source_id="roll-v1",
            source_version="hash-old", title="卷梗概（第1-3章）", status="active",
            authority_level="derived", chapter_from=1, chapter_to=3,
            created_at=now, updated_at=now,
        ))
        await session.flush()
        for index in range(2):
            session.add(RetrievalChunkModel(
                id=f"ck-v1-{index}", project_id="p1", document_id="doc-v1", chunk_index=index,
                content=f"梗概块{index}", summary="", metadata_json="{}", search_text=f"梗概块{index}",
                embedding=None, embedding_model="none", embedding_version="v1",
                token_count=4, content_hash=f"ch{index}", status="active",
                created_at=now, updated_at=now,
            ))
        await session.commit()


async def _rollup_by_id(session_factory, rollup_id: str) -> RecapRollupModel:
    async with session_factory() as session:
        return await session.get(RecapRollupModel, rollup_id)


async def _audits(session_factory, action: str) -> list[AuditLogModel]:
    async with session_factory() as session:
        return list((await session.scalars(select(AuditLogModel).where(AuditLogModel.action == action))).all())


async def _jobs(session_factory, job_type: str) -> list[RetrievalJobModel]:
    async with session_factory() as session:
        return list((await session.scalars(select(RetrievalJobModel).where(RetrievalJobModel.job_type == job_type))).all())


# -- set_active_version funnel -------------------------------------------------


@pytest.mark.asyncio
async def test_set_active_version_invalidates_covering_rollups_same_commit(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3])
    await _seed_rollups_with_index(session_factory)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        version = await uow.chapters.append_version(chapter_id="ch2", content="第2章修订后正文")
        await uow.chapters.set_active_version("ch2", version.id)
        await uow.commit()
        new_version_id = version.id

    # Covering rollups (volume 1-3, book, era) stale; the 4-6 volume untouched.
    assert (await _rollup_by_id(session_factory, "roll-v1")).stale is True
    assert (await _rollup_by_id(session_factory, "roll-book")).stale is True
    assert (await _rollup_by_id(session_factory, "roll-era")).stale is True
    assert (await _rollup_by_id(session_factory, "roll-v2")).stale is False

    # One recap.stale audit per invalidated recap, carrying the revised chapter.
    audits = await _audits(session_factory, "recap.stale")
    assert {row.target_id for row in audits} == {"roll-v1", "roll-book", "roll-era"}
    volume_audit = next(row for row in audits if row.target_id == "roll-v1")
    payload = json.loads(volume_audit.payload)
    assert payload["revised_chapter"] == 2 and payload["version_id"] == new_version_id
    assert payload["superseded_chunks"] == 2

    # The summary job for the NEW active version rides the same commit.
    summarize_jobs = await _jobs(session_factory, "summarize_chapter")
    assert len(summarize_jobs) == 1
    assert summarize_jobs[0].source_type == "chapter_version"
    assert summarize_jobs[0].source_id == new_version_id
    assert summarize_jobs[0].status == "pending"

    # The funnel also enqueues the chapter indexing job in the same commit —
    # call sites that never queued one (tasks / executor / importer) are
    # covered centrally now.
    index_jobs = await _jobs(session_factory, "index_chapter")
    assert len(index_jobs) == 1
    assert index_jobs[0].source_type == "chapter"
    assert index_jobs[0].source_id == "ch2"
    assert index_jobs[0].status == "pending"

    # Index layer: the stale recap's document is inactive, chunks superseded —
    # outdated recaps can no longer leak back as retrieval evidence.
    async with session_factory() as session:
        document = await session.get(RetrievalDocumentModel, "doc-v1")
        assert document.status == "inactive"
        chunk_states = (await session.scalars(
            select(RetrievalChunkModel.status).where(RetrievalChunkModel.document_id == "doc-v1")
        )).all()
        assert set(chunk_states) == {"superseded"}


@pytest.mark.asyncio
async def test_stale_marking_is_idempotent(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3])
    await _seed_rollups_with_index(session_factory)

    for round_no in range(2):
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            version = await uow.chapters.append_version(chapter_id="ch2", content=f"第2章第{round_no}次修订")
            await uow.chapters.set_active_version("ch2", version.id)
            await uow.commit()

    # Already-stale rollups are not re-audited on the second revision.
    assert len(await _audits(session_factory, "recap.stale")) == 3
    # Each revision still re-enqueues the summary for its own new version.
    assert len(await _jobs(session_factory, "summarize_chapter")) == 2
    # Index jobs dedupe on the shared pending row (get-or-create): rapid
    # successive revisions do not stack duplicate index_chapter jobs.
    assert len(await _jobs(session_factory, "index_chapter")) == 1


@pytest.mark.asyncio
async def test_approve_proposal_call_site_goes_through_the_funnel(session_factory):
    """Representative call site: proposal approval writes back via the same
    set_active_version funnel, so its covering rollups go stale too."""
    await _seed_chapters(session_factory, [1, 2, 3])
    await _seed_rollups_with_index(session_factory)
    now = datetime.now(UTC)
    async with session_factory() as session:
        base = await session.get(ChapterVersionModel, "v2")
        session.add(RevisionProposalModel(
            id="prop-1", chapter_id="ch2", base_version_id="v2",
            before_hash=base.content_hash, after_text="第2章批准稿", after_hash="ah",
            rationale="r", status="PROPOSED",
            hunks_json=json.dumps([{"start": 0, "end": 5, "replacement": "第2章批准稿"}]),
            created_at=now, updated_at=now,
        ))
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        result = await approve_persisted_proposal(uow=uow, proposal_id="prop-1", user_id="u1", idempotency_key=None)
        await uow.commit()

    assert result.replayed is False
    assert (await _rollup_by_id(session_factory, "roll-v1")).stale is True
    assert (await _rollup_by_id(session_factory, "roll-book")).stale is True
    assert (await _rollup_by_id(session_factory, "roll-v2")).stale is False
    # Approval's own summarize job + the funnel's — both pending, both for the
    # new version; the summarizer's per-version idempotency dedupes downstream.
    summarize_jobs = await _jobs(session_factory, "summarize_chapter")
    assert len(summarize_jobs) >= 1
    assert {job.source_id for job in summarize_jobs} == {result.version.id}
    # The funnel created the index job; approve_proposal's manual enqueue
    # dedupes onto it (get-or-create) — exactly ONE index_chapter row.
    index_jobs = await _jobs(session_factory, "index_chapter")
    assert len(index_jobs) == 1
    assert index_jobs[0].source_type == "chapter" and index_jobs[0].source_id == "ch2"


# -- legacy importer write path (funnel coverage spot-check) ---------------------


@pytest.mark.asyncio
async def test_legacy_import_path_enqueues_index_chapter_via_funnel(session_factory, tmp_path):
    """The legacy importer writes chapters through set_active_version without
    ever queueing indexing itself; the funnel must queue one index_chapter
    job per imported chapter (multi-version chapters collapse to one row)."""
    import sqlite3

    from proseforge.infrastructure.legacy_import.importer import LegacyImporter

    slot = tmp_path / "slot-legacy"
    (slot / "outlines").mkdir(parents=True)
    (slot / "reports").mkdir()
    (slot / "project.json").write_text(json.dumps({"title": "Imported"}), encoding="utf-8")
    connection = sqlite3.connect(slot / "novel.db")
    connection.executescript("""
        CREATE TABLE chapters (id TEXT PRIMARY KEY, chapter_no INTEGER, title TEXT);
        CREATE TABLE chapter_versions (chapter_id TEXT, version_no INTEGER, content TEXT);
        INSERT INTO chapters VALUES ('c1', 1, 'One'), ('c2', 2, 'Two');
        INSERT INTO chapter_versions VALUES ('c1', 1, 'old'), ('c1', 2, 'latest'), ('c2', 1, 'only');
    """)
    connection.commit()
    connection.close()

    report = await LegacyImporter(
        tmp_path / "archive", session_factory=session_factory, owner_id="u1"
    ).import_workspace(tmp_path)

    assert report.status == "COMPLETED"
    assert report.chapters_imported == 2
    # Two imported chapters -> exactly two pending index jobs (c1's two
    # versions dedupe onto one shared pending row).
    index_jobs = await _jobs(session_factory, "index_chapter")
    assert len(index_jobs) == 2
    assert all(job.status == "pending" and job.source_type == "chapter" for job in index_jobs)
    assert len({job.source_id for job in index_jobs}) == 2


# -- stale recaps stay out of the scene pack ------------------------------------


@pytest.mark.asyncio
async def test_scene_state_injects_only_settled_recaps(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3])
    async with session_factory() as session:
        session.add(_rollup("roll-fresh", "volume", 1, 3))
        session.add(_rollup("roll-stale", "book", 1, 3, stale=True))
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        state = await build_scene_state(uow, "p1", "u1")

    assert [recap["level"] for recap in state["recaps"]] == ["volume"]
    assert state["recaps"][0]["content"] == "volume梗概 1-3"


# -- lazy recompute enqueue ------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_recompute_enqueues_for_stale_volumes(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3, 4])
    async with session_factory() as session:
        session.add(_rollup("roll-v1", "volume", 1, 3, stale=True))
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        job_ids = await enqueue_stale_recap_recompute(uow, project_id="p1", chapter_no=4, user_id="u1")
        await uow.commit()

    assert len(job_ids) == 1
    jobs = await _jobs(session_factory, "rollup_recap")
    assert len(jobs) == 1 and jobs[0].source_id == "ch3" and jobs[0].status == "pending"
    audits = await _audits(session_factory, "recap.recompute_queued")
    assert len(audits) == 1
    payload = json.loads(audits[0].payload)
    assert payload["span_start"] == 1 and payload["span_end"] == 3 and payload["trigger_chapter"] == 4

    # In-flight dedup: a second scene-pack build enqueues nothing more.
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await enqueue_stale_recap_recompute(uow, project_id="p1", chapter_no=4, user_id="u1") == []


@pytest.mark.asyncio
async def test_lazy_recompute_ignores_unfinished_and_fresh_volumes(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3])
    async with session_factory() as session:
        session.add(_rollup("roll-fresh", "volume", 1, 3))  # not stale
        session.add(_rollup("roll-stale-book", "book", 1, 3, stale=True))  # book level: healed by the volume job
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        # chapter_no=3: the volume closes AT 3 (span_end not < chapter_no) and
        # is not stale anyway; stale book recaps never enqueue directly.
        assert await enqueue_stale_recap_recompute(uow, project_id="p1", chapter_no=3, user_id="u1") == []
        await uow.commit()
    assert await _jobs(session_factory, "rollup_recap") == []


@pytest.mark.asyncio
async def test_lazy_recompute_skips_volume_without_end_chapter(session_factory):
    await _seed_chapters(session_factory, [1, 2, 3])
    async with session_factory() as session:
        session.add(_rollup("roll-v9", "volume", 7, 9, stale=True))  # chapter 9 does not exist
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await enqueue_stale_recap_recompute(uow, project_id="p1", chapter_no=10, user_id="u1") == []
        await uow.commit()
    assert await _jobs(session_factory, "rollup_recap") == []
