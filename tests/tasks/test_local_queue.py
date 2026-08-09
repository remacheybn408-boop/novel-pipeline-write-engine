"""LocalTaskQueue 的持久化/并发/租约语义测试（V15-004）。

全部跑在 SQLite 临时文件上（create_sqlite_engine，WAL + busy_timeout）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.sqlite import create_sqlite_engine
from proseforge.infrastructure.tasks.local import (
    DEFAULT_MAX_ATTEMPTS,
    LocalTaskQueue,
    TaskEventModel,
    TaskJobModel,
)

_TABLES = [TaskJobModel.__table__, TaskEventModel.__table__]


async def _make_queue(db_path: Path, **queue_kwargs):
    """建引擎 + 建表 + LocalTaskQueue；返回 (engine, queue) 由测试自行清理。"""
    engine = create_sqlite_engine(db_path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=_TABLES)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    queue = LocalTaskQueue(session_factory, **queue_kwargs)
    return engine, queue


async def _wait_for_status(queue: LocalTaskQueue, job_id: str, status: str, timeout: float = 5.0):
    async def _poll():
        while True:
            job = await queue.get_job(job_id)
            if job is not None and job["status"] == status:
                return job
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_enqueue_persists_across_queue_rebuild(tmp_path: Path):
    """写 job → 引擎销毁 → 新引擎/新队列（模拟进程重启）→ job 还在。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path)
    job_id = await queue.enqueue("proseforge.healthcheck", {"origin": "first-process"})
    await engine.dispose()

    rebuilt_engine, rebuilt_queue = await _make_queue(db_path)
    try:
        job = await rebuilt_queue.get_job(job_id)
        assert job is not None
        assert job["task_name"] == "proseforge.healthcheck"
        assert job["payload"] == {"origin": "first-process"}
        assert job["status"] == "PENDING"
        assert job["attempts"] == 0
        # 新进程可以直接 claim 这个 job——DB 是唯一事实源。
        claimed = await rebuilt_queue.claim()
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.payload == {"origin": "first-process"}
    finally:
        await rebuilt_engine.dispose()


@pytest.mark.asyncio
async def test_only_one_claim_between_competing_workers(tmp_path: Path):
    """两个独立引擎（模拟两个进程）同时 claim 同一 job，只有一个成功。"""
    db_path = tmp_path / "queue.db"
    engine_a, queue_a = await _make_queue(db_path)
    engine_b, queue_b = await _make_queue(db_path)
    try:
        job_id = await queue_a.enqueue("proseforge.healthcheck", {})
        first, second = await asyncio.gather(queue_a.claim(), queue_b.claim())
        claimed = [job for job in (first, second) if job is not None]
        assert len(claimed) == 1
        assert claimed[0].id == job_id
        # 已被认领（RUNNING 带未过期 lease）后，任何一方都不能再 claim。
        assert await queue_a.claim() is None
        assert await queue_b.claim() is None
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


@pytest.mark.asyncio
async def test_recover_expired_returns_job_to_pending_with_backoff(tmp_path: Path):
    """RUNNING 且 lease 过期 → recover → 回 PENDING 带指数退避；attempts 由 claim 递增。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path, lease_seconds=60.0)
    try:
        job_id = await queue.enqueue("proseforge.healthcheck", {})
        claimed = await queue.claim()
        assert claimed is not None
        job = await queue.get_job(job_id)
        assert job is not None and job["status"] == "RUNNING"
        assert job["attempts"] == 1  # claim 即递增 attempts（local.py:199）
        assert job["lease_expires_at"] is not None

        # lease 尚未过期：recover 不动它。
        assert await queue.recover_expired() == 0
        job = await queue.get_job(job_id)
        assert job is not None and job["status"] == "RUNNING"

        # 时间快进到 lease 过期之后：回 PENDING，available_at 推后
        # 2**max(0, attempts-1) = 2**0 = 1 秒（local.py:250）。
        future = datetime.now(UTC) + timedelta(seconds=120.0)
        assert await queue.recover_expired(now=future) == 1
        job = await queue.get_job(job_id)
        assert job is not None
        assert job["status"] == "PENDING"
        assert job["attempts"] == 1  # recover 不递增 attempts
        assert job["lease_expires_at"] is None
        expected_due = (future + timedelta(seconds=1)).replace(tzinfo=None)
        actual_due = job["available_at"].replace(tzinfo=None)
        assert abs((actual_due - expected_due).total_seconds()) < 1.0
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "claimed", "recovered"]

        # 退避未到期：claim 拿不到（local.py:188 的 available_at <= now 谓词）。
        assert await queue.claim() is None

        # 退避到期后可重新 claim，attempts 再 +1。
        async with engine.begin() as connection:
            await connection.execute(
                update(TaskJobModel)
                .where(TaskJobModel.id == job_id)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        reclaimed = await queue.claim()
        assert reclaimed is not None and reclaimed.id == job_id
        assert reclaimed.attempts == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_expired_marks_failed_after_max_attempts(tmp_path: Path):
    """attempts 已达 DEFAULT_MAX_ATTEMPTS 的 RUNNING job lease 过期 → 直接 FAILED。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path, lease_seconds=60.0)
    try:
        job_id = await queue.enqueue("proseforge.healthcheck", {})
        assert await queue.claim() is not None
        async with engine.begin() as connection:
            await connection.execute(
                update(TaskJobModel).where(TaskJobModel.id == job_id).values(attempts=DEFAULT_MAX_ATTEMPTS)
            )
        future = datetime.now(UTC) + timedelta(seconds=120.0)
        # 达到上限的 job 走 FAILED 分支，不计入 recovered（local.py:243-246）。
        assert await queue.recover_expired(now=future) == 0
        job = await queue.get_job(job_id)
        assert job is not None
        assert job["status"] == "FAILED"
        assert job["last_error"] == "lease expired after maximum attempts"
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "claimed", "failed"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_before_claim(tmp_path: Path):
    """PENDING 可取消；CANCELLED 不可再 claim，也不可重复取消。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path)
    try:
        job_id = await queue.enqueue("proseforge.healthcheck", {})
        assert await queue.cancel(job_id) is True
        job = await queue.get_job(job_id)
        assert job is not None and job["status"] == "CANCELLED"
        assert await queue.claim() is None
        assert await queue.cancel(job_id) is False
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "cancelled"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cannot_cancel_after_claim(tmp_path: Path):
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path)
    try:
        job_id = await queue.enqueue("proseforge.healthcheck", {})
        assert await queue.claim() is not None
        assert await queue.cancel(job_id) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_finishes_job_with_full_event_trail(tmp_path: Path):
    """start 后 worker 派发已注册 handler：SUCCEEDED，事件链完整，payload 到达。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path, concurrency=1, poll_seconds=0.02)
    received: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> str:
        received.append(payload)
        return "ok"

    queue.register("test.echo", handler)
    try:
        await queue.start()
        job_id = await queue.enqueue("test.echo", {"value": 42})
        job = await _wait_for_status(queue, job_id, "SUCCEEDED")
        assert job["last_error"] is None
        assert job["lease_expires_at"] is None
        assert received == [{"value": 42}]
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "claimed", "succeeded"]
        # 完成的 job 保留在 task_jobs 中可审计（不会被删除）。
        assert (await queue.get_job(job_id)) is not None
    finally:
        await queue.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_marks_failed_job_with_last_error(tmp_path: Path):
    """handler 抛错 → worker 走 retry：前 MAX_ATTEMPTS-1 次 release 回 PENDING
    带退避，最后一次转 FAILED（local.py:279-312，worker.py:90）。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(db_path, concurrency=1, poll_seconds=0.02)

    async def handler(payload: dict[str, object]) -> str:
        raise ValueError("boom")

    queue.register("test.fail", handler)
    try:
        await queue.start()
        job_id = await queue.enqueue("test.fail", {})
        # 退避 1s + 2s，留足余量等第三次尝试落 FAILED。
        job = await _wait_for_status(queue, job_id, "FAILED", timeout=15.0)
        assert job["last_error"] is not None
        assert "ValueError" in job["last_error"]
        assert job["attempts"] == DEFAULT_MAX_ATTEMPTS
        # released 与 failed 事件分离：重试放记录 released，终态记 failed。
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "claimed", "released", "claimed", "released", "claimed", "failed"]
    finally:
        await queue.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_graceful_stop_releases_lease(tmp_path: Path):
    """stop：停新任务、grace 期后 cancel 在跑的 handler 并释放 lease（回 PENDING）。"""
    db_path = tmp_path / "queue.db"
    engine, queue = await _make_queue(
        db_path, concurrency=1, poll_seconds=0.02, stop_grace_seconds=0.1
    )
    started = asyncio.Event()
    never = asyncio.Event()

    async def handler(payload: dict[str, object]) -> str:
        started.set()
        await never.wait()  # 永不完成，等 stop 的 grace 期超时后被 cancel
        return "done"

    queue.register("test.block", handler)
    try:
        await queue.start()
        job_id = await queue.enqueue("test.block", {})
        await asyncio.wait_for(started.wait(), timeout=5.0)
        job = await _wait_for_status(queue, job_id, "RUNNING")
        assert job["lease_expires_at"] is not None
        await queue.stop()
        job = await queue.get_job(job_id)
        assert job is not None
        assert job["status"] == "PENDING"
        assert job["lease_expires_at"] is None
        assert job["attempts"] == 1  # claim 已 +1（local.py:199）；release 不再递增（local.py:317）
        event_types = [event["event_type"] for event in await queue.list_events(job_id)]
        assert event_types == ["enqueued", "claimed", "released"]
        # 释放后可被重新 claim，attempts 再 +1。
        reclaimed = await queue.claim()
        assert reclaimed is not None and reclaimed.id == job_id
        assert reclaimed.attempts == 2
    finally:
        await queue.stop()
        await engine.dispose()
