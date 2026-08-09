"""Durable local task queue（V15-004，native profile）。

数据库是唯一事实源：enqueue 写 task_jobs/task_events，进程重启后
未完成的 job 仍在表中，可被新进程重新 claim。

claim 的 only-one 语义（SQLite 与 PostgreSQL 均成立）：
claim 是一个事务——SELECT 到期 PENDING 候选 → 条件 UPDATE
``WHERE id=:id AND status='PENDING'`` 置 RUNNING 并带 lease →
写 claimed 事件 → COMMIT。模型代码（handler）在 COMMIT 之后、
锁已释放时才执行。

- PostgreSQL（READ COMMITTED）：条件 UPDATE 取行锁；并发 claimer 阻塞
  到胜者 COMMIT 后重估谓词，rowcount=0 → 回滚重试。
- SQLite（WAL，busy_timeout=5000）：同一时刻只有一个写者。败者要么
  在等锁后看到 status 已变（rowcount=0），要么因快照过期拿到
  SQLITE_BUSY(_SNAPSHOT) 抛 OperationalError——两者都按"未抢到"处理。

时间戳一律写 UTC aware datetime；SQLite 方言按固定格式存取，SQL 层
比较在两个方言上语义一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Index, Integer, String, Text, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.tasks.worker import LocalQueueWorker, TaskHandler

if TYPE_CHECKING:
    from collections.abc import Sequence

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"

EVENT_ENQUEUED = "enqueued"
EVENT_CLAIMED = "claimed"
EVENT_RECOVERED = "recovered"
EVENT_RELEASED = "released"
EVENT_CANCELLED = "cancelled"
EVENT_SUCCEEDED = "succeeded"
EVENT_FAILED = "failed"

DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_STOP_GRACE_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskJobModel(Base):
    __tablename__ = "task_jobs"
    __table_args__ = (
        Index("ix_task_jobs_status_available", "status", "available_at"),
        Index("ix_task_jobs_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class TaskEventModel(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        Index("ix_task_events_task_created", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    task_name: str
    payload: dict[str, object]
    attempts: int
    lease_token: int


class LocalTaskQueue:
    """TaskQueue port 的本地持久实现（native profile）。

    并发 worker 数来自 Settings.native_worker_concurrency，轮询间隔来自
    Settings.native_queue_poll_seconds（由 factory 注入）。完成的 job
    （SUCCEEDED/FAILED/CANCELLED）保留在 task_jobs 中可审计。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        concurrency: int = 2,
        poll_seconds: float = 1.0,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        stop_grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
        worker_id: str | None = None,
    ):
        self._session_factory = session_factory
        self._concurrency = concurrency
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self.worker_id = worker_id or f"local:{new_id()}"
        self._handlers: dict[str, TaskHandler] = {}
        self._worker: LocalQueueWorker | None = None

    @property
    def registered_task_names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def register(self, task_name: str, handler: TaskHandler) -> None:
        """注册任务名 → async callable(payload)。"""
        self._handlers[task_name] = handler

    async def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        now = _utcnow()
        job_id = new_id()
        async with self._session_factory() as session:
            session.add(TaskJobModel(
                id=job_id,
                task_name=task_name,
                payload_json=json.dumps(payload),
                status=STATUS_PENDING,
                attempts=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            ))
            session.add(self._event(job_id, EVENT_ENQUEUED, {"task_name": task_name}, now))
            await session.commit()
        return job_id

    async def cancel(self, task_id: str) -> bool:
        """claim 前可取消（PENDING→CANCELLED）；已 claim 的 job 返回 False。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(TaskJobModel)
                .where(TaskJobModel.id == task_id, TaskJobModel.status == STATUS_PENDING)
                .values(status=STATUS_CANCELLED, updated_at=now)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            session.add(self._event(task_id, EVENT_CANCELLED, {}, now))
            await session.commit()
            return True

    async def claim(self) -> ClaimedJob | None:
        """认领一个到期 PENDING job：单事务完成 选行→置 RUNNING 带 lease→commit。

        handler 绝不在此事务内执行。争锁失败（并发 claim 败者）返回 None，
        由 worker 下一轮轮询重试。
        """
        now = _utcnow()
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            try:
                job_id = (
                    await session.execute(
                        select(TaskJobModel.id)
                        .where(TaskJobModel.status == STATUS_PENDING, TaskJobModel.available_at <= now)
                        .order_by(TaskJobModel.created_at, TaskJobModel.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if job_id is None:
                    await session.rollback()
                    return None
                claimed = await session.execute(
                    update(TaskJobModel)
                    .where(TaskJobModel.id == job_id, TaskJobModel.status == STATUS_PENDING)
                    .values(status=STATUS_RUNNING, attempts=TaskJobModel.attempts + 1, lease_expires_at=lease_until, lease_token=TaskJobModel.lease_token + 1, updated_at=now)
                )
                if claimed.rowcount != 1:
                    await session.rollback()
                    return None
                session.add(self._event(job_id, EVENT_CLAIMED, {"worker_id": self.worker_id}, now))
                await session.commit()
            except OperationalError:
                # SQLite 写锁争用（BUSY / BUSY_SNAPSHOT）：本轮未抢到。
                await session.rollback()
                return None
        # 锁已随 commit 释放；以下读取不在任何事务锁内。
        async with self._session_factory() as session:
            row = (
                await session.execute(select(TaskJobModel).where(TaskJobModel.id == job_id))
            ).scalar_one_or_none()
        if row is None or row.status != STATUS_RUNNING:
            return None
        return ClaimedJob(
            id=row.id,
            task_name=row.task_name,
            payload=json.loads(row.payload_json),
            attempts=row.attempts,
            lease_token=row.lease_token,
        )

    async def recover_expired(self, *, now: datetime | None = None) -> int:
        """把 lease 过期的 RUNNING job 放回 PENDING（带指数退避）。

        attempts 由 claim 递增；此处已达 DEFAULT_MAX_ATTEMPTS 的 job 直接
        转 FAILED（failed 事件），其余回 PENDING 并把 available_at 推后
        2**max(0, attempts-1) 秒（recovered 事件）。返回放回 PENDING 的数量。
        """
        now = now or _utcnow()
        async with self._session_factory() as session:
            try:
                rows = list((
                    await session.execute(
                        select(TaskJobModel).where(
                            TaskJobModel.status == STATUS_RUNNING,
                            TaskJobModel.lease_expires_at <= now,
                        )
                    )
                ).scalars().all())
                if not rows:
                    await session.rollback()
                    return 0
                recovered = 0
                for row in rows:
                    if row.attempts >= DEFAULT_MAX_ATTEMPTS:
                        row.status = STATUS_FAILED
                        row.last_error = "lease expired after maximum attempts"
                        session.add(self._event(row.id, EVENT_FAILED, {"worker_id": self.worker_id, "error": row.last_error}, now))
                    else:
                        row.status = STATUS_PENDING
                        row.lease_expires_at = None
                        row.available_at = now + timedelta(seconds=2 ** max(0, row.attempts - 1))
                        session.add(self._event(row.id, EVENT_RECOVERED, {"worker_id": self.worker_id}, now))
                        recovered += 1
                    row.updated_at = now
                await session.commit()
                return recovered
            except OperationalError:
                await session.rollback()
                return 0

    async def complete(self, task_id: str, lease_token: int) -> bool:
        return await self._finish(task_id, lease_token, STATUS_SUCCEEDED, EVENT_SUCCEEDED, None)

    async def fail(self, task_id: str, lease_token: int, error: str) -> bool:
        return await self._finish(task_id, lease_token, STATUS_FAILED, EVENT_FAILED, error)

    async def retry(self, task_id: str, lease_token: int, error: str) -> bool:
        now = _utcnow()
        async with self._session_factory() as session:
            attempts = await session.scalar(
                select(TaskJobModel.attempts).where(
                    TaskJobModel.id == task_id,
                    TaskJobModel.status == STATUS_RUNNING,
                    TaskJobModel.lease_token == lease_token,
                )
            )
            if attempts is None:
                await session.rollback()
                return False
            terminal = await session.execute(
                update(TaskJobModel)
                .where(
                    TaskJobModel.id == task_id,
                    TaskJobModel.status == STATUS_RUNNING,
                    TaskJobModel.lease_token == lease_token,
                    TaskJobModel.attempts >= DEFAULT_MAX_ATTEMPTS,
                )
                .values(status=STATUS_FAILED, last_error=error, lease_expires_at=None, updated_at=now)
            )
            if terminal.rowcount == 1:
                event_type = EVENT_FAILED
            else:
                retry = await session.execute(
                    update(TaskJobModel)
                    .where(
                        TaskJobModel.id == task_id,
                        TaskJobModel.status == STATUS_RUNNING,
                        TaskJobModel.lease_token == lease_token,
                        TaskJobModel.attempts < DEFAULT_MAX_ATTEMPTS,
                    )
                    .values(
                        status=STATUS_PENDING,
                        last_error=error,
                        lease_expires_at=None,
                        available_at=now + timedelta(seconds=2 ** max(0, int(attempts) - 1)),
                        updated_at=now,
                    )
                )
                if retry.rowcount != 1:
                    await session.rollback()
                    return False
                event_type = EVENT_RELEASED
            session.add(self._event(task_id, event_type, {"worker_id": self.worker_id, "error": error}, now))
            await session.commit()
            return True

    async def release(self, task_id: str, lease_token: int) -> bool:
        """释放 lease：RUNNING→PENDING，不增加 attempts（graceful stop 用）。"""
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(TaskJobModel)
                .where(TaskJobModel.id == task_id, TaskJobModel.status == STATUS_RUNNING, TaskJobModel.lease_token == lease_token)
                .values(status=STATUS_PENDING, lease_expires_at=None, updated_at=now)
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            session.add(self._event(task_id, EVENT_RELEASED, {"worker_id": self.worker_id}, now))
            await session.commit()
            return True

    async def get_job(self, task_id: str) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(select(TaskJobModel).where(TaskJobModel.id == task_id))
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "task_name": row.task_name,
            "payload": json.loads(row.payload_json),
            "status": row.status,
            "attempts": row.attempts,
            "lease_token": row.lease_token,
            "available_at": row.available_at,
            "lease_expires_at": row.lease_expires_at,
            "last_error": row.last_error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def list_events(self, task_id: str) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows: Sequence[TaskEventModel] = (
                await session.execute(
                    select(TaskEventModel)
                    .where(TaskEventModel.task_id == task_id)
                    .order_by(TaskEventModel.created_at, TaskEventModel.id)
                )
            ).scalars().all()
        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "event_type": row.event_type,
                "payload": json.loads(row.payload_json),
                "created_at": row.created_at,
            }
            for row in rows
        ]

    async def start(self) -> None:
        """启动有界 asyncio worker 池（幂等）。"""
        if self._worker is not None:
            return
        worker = LocalQueueWorker(
            self,
            self._handlers,
            concurrency=self._concurrency,
            poll_seconds=self._poll_seconds,
            grace_seconds=self._stop_grace_seconds,
        )
        await worker.start()
        self._worker = worker

    async def stop(self) -> None:
        """graceful stop：停新任务、等 grace 期、释放未完成 job 的 lease。"""
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker.stop()

    async def _finish(self, task_id: str, lease_token: int, status: str, event_type: str, error: str | None) -> bool:
        now = _utcnow()
        async with self._session_factory() as session:
            result = await session.execute(
                update(TaskJobModel)
                .where(TaskJobModel.id == task_id, TaskJobModel.status == STATUS_RUNNING, TaskJobModel.lease_token == lease_token)
                .values(
                    status=status,
                    lease_expires_at=None,
                    last_error=error,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                return False
            payload = {"worker_id": self.worker_id}
            if error is not None:
                payload["error"] = error
            session.add(self._event(task_id, event_type, payload, now))
            await session.commit()
            return True

    @staticmethod
    def _event(task_id: str, event_type: str, payload: dict[str, object], now: datetime) -> TaskEventModel:
        return TaskEventModel(
            id=new_id(),
            task_id=task_id,
            event_type=event_type,
            payload_json=json.dumps(payload),
            created_at=now,
        )
