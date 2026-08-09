"""Retrieval indexing sweeper: stale pending jobs are re-dispatched.

Covers the crash window between the business commit (retrieval_jobs row)
and the queue enqueue: sweep_pending_jobs re-enqueues rows still pending
past the threshold and leaves fresh/non-pending rows alone. A recording
fake queue stands in for the real TaskQueue port.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.retrieval.indexing import sweep_pending_jobs
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import RetrievalJobModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.conftest import make_fk_engine

_TABLES = [
    UserModel.__table__,
    ProjectModel.__table__,
    RetrievalJobModel.__table__,
]


class RecordingQueue:
    """TaskQueue test double: captures enqueue calls, optionally failing."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        if self.fail:
            raise ConnectionError("queue down")
        self.calls.append((task_name, payload))
        return f"task-{len(self.calls)}"


@pytest_asyncio.fixture()
async def session_factory():
    engine = make_fk_engine()
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(UserModel(id="user-1", email="u@example.com", password_hash="x", role="USER", session_version=1))
        session.add(ProjectModel(id="proj-1", owner_id="user-1", slug="novel", title="Novel"))
        await session.commit()
    yield factory
    await engine.dispose()


async def _add_job(
    session_factory,
    job_id: str,
    *,
    status: str = "pending",
    age_seconds: float = 0,
    started_age_seconds: float | None = None,
    job_type: str = "index_chapter",
) -> None:
    requested_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    started_at = (
        datetime.now(UTC) - timedelta(seconds=started_age_seconds)
        if started_age_seconds is not None
        else None
    )
    async with session_factory() as session:
        session.add(
            RetrievalJobModel(
                id=job_id,
                project_id="proj-1",
                job_type=job_type,
                source_type="chapter",
                source_id=f"src-{job_id}",
                status=status,
                attempt=0,
                requested_at=requested_at,
                started_at=started_at,
            )
        )
        await session.commit()


async def _sweep(session_factory, queue, **kwargs) -> int:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        return await sweep_pending_jobs(uow, queue, **kwargs)


@pytest.mark.asyncio
async def test_stale_pending_job_is_redispatched(session_factory):
    await _add_job(session_factory, "job-stale", age_seconds=600)
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 1
    assert queue.calls == [
        ("proseforge.retrieval.index_document", {"job_id": "job-stale", "user_id": "user-1"})
    ]


@pytest.mark.asyncio
async def test_fresh_pending_job_is_not_redispatched(session_factory):
    await _add_job(session_factory, "job-fresh", age_seconds=60)
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 0
    assert queue.calls == []


@pytest.mark.asyncio
async def test_sweep_counts_only_stale_pending_jobs(session_factory):
    await _add_job(session_factory, "job-stale-1", age_seconds=900)
    await _add_job(session_factory, "job-stale-2", age_seconds=600)
    await _add_job(session_factory, "job-fresh", age_seconds=10)
    await _add_job(session_factory, "job-done", status="done", age_seconds=900)
    # Fresh running job (started 60s ago, below the 600s running threshold):
    # legitimately in flight, must be left alone.
    await _add_job(session_factory, "job-running", status="running", age_seconds=900, started_age_seconds=60)
    await _add_job(session_factory, "job-failed", status="failed", age_seconds=900)
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 2
    assert {payload["job_id"] for _, payload in queue.calls} == {"job-stale-1", "job-stale-2"}


@pytest.mark.asyncio
async def test_stale_running_job_is_rearmed_and_redispatched(session_factory):
    """Worker killed mid-job strands the row in running (claim_job only
    transitions pending -> running, so nothing resets it). The sweeper
    re-arms it to pending and redispatches; the chapter gets indexed on
    the next worker pass instead of staying outside the index forever."""
    await _add_job(session_factory, "job-stuck", status="running", age_seconds=900, started_age_seconds=900)
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 1
    assert queue.calls == [
        ("proseforge.retrieval.index_document", {"job_id": "job-stuck", "user_id": "user-1"})
    ]
    async with session_factory() as session:
        job = await session.get(RetrievalJobModel, "job-stuck")
        assert job.status == "pending"

    # requested_at was re-stamped on re-arm/dispatch: no duplicate next sweep.
    queue_followup = RecordingQueue()
    assert await _sweep(session_factory, queue_followup, threshold_seconds=300) == 0
    assert queue_followup.calls == []


@pytest.mark.asyncio
async def test_sweep_respects_limit(session_factory):
    for index in range(3):
        await _add_job(session_factory, f"job-{index}", age_seconds=600)
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300, limit=2)

    assert redispatched == 2
    assert len(queue.calls) == 2


@pytest.mark.asyncio
async def test_queue_failure_stops_sweep_without_raising(session_factory):
    await _add_job(session_factory, "job-stale", age_seconds=600)
    queue = RecordingQueue(fail=True)

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 0
    # Row stays pending, so the next sweep retries the dispatch.
    async with session_factory() as session:
        job = await session.get(RetrievalJobModel, "job-stale")
        assert job.status == "pending"


@pytest.mark.asyncio
async def test_redispatched_job_requested_at_is_bumped(session_factory):
    """After a successful re-dispatch the row's requested_at is re-stamped,
    so an immediate follow-up sweep does not enqueue a duplicate."""
    await _add_job(session_factory, "job-stale", age_seconds=600)
    queue = RecordingQueue()

    assert await _sweep(session_factory, queue, threshold_seconds=300) == 1

    async with session_factory() as session:
        job = await session.get(RetrievalJobModel, "job-stale")
        assert job.status == "pending"
        requested_at = job.requested_at
        if requested_at.tzinfo is None:  # sqlite reads back naive datetimes
            requested_at = requested_at.replace(tzinfo=UTC)
        assert datetime.now(UTC) - requested_at < timedelta(seconds=300)

    queue_followup = RecordingQueue()
    assert await _sweep(session_factory, queue_followup, threshold_seconds=300) == 0
    assert queue_followup.calls == []


@pytest.mark.asyncio
async def test_sweep_routes_each_job_type_to_its_own_task(session_factory):
    """Every retrieval_jobs row has its own worker lane: before job_type
    routing the sweeper sent everything to the indexing handler, and
    stranded rollup_recap/summarize_chapter rows died as source-not-found
    in the wrong worker."""
    await _add_job(session_factory, "job-index", age_seconds=600, job_type="index_chapter")
    await _add_job(session_factory, "job-recap-index", age_seconds=600, job_type="index_recap")
    await _add_job(session_factory, "job-rollup", age_seconds=600, job_type="rollup_recap")
    await _add_job(session_factory, "job-summarize", age_seconds=600, job_type="summarize_chapter")
    queue = RecordingQueue()

    redispatched = await _sweep(session_factory, queue, threshold_seconds=300)

    assert redispatched == 4
    task_by_job = {payload["job_id"]: task_name for task_name, payload in queue.calls}
    assert task_by_job == {
        "job-index": "proseforge.retrieval.index_document",
        "job-recap-index": "proseforge.retrieval.index_document",
        "job-rollup": "proseforge.work.rollup_recap",
        "job-summarize": "proseforge.work.summarize_chapter",
    }
