"""TaskQueue 工厂（V15-004）。

按 runtime profile 的 queue capability 装配队列：
native→LocalTaskQueue（注入 Settings 的 worker 并发/轮询参数，并注册
workflows.tasks.HANDLERS 中的全部任务 handler）、server→CeleryTaskQueue、
test→InMemoryTaskQueue。server 绝不回退 SQLite/本地队列——profile 与
database_url 的非法组合在 Settings 校验层已经抛错，这里不做任何兜底。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.domain.ports.task_queue import TaskQueue
from proseforge.infrastructure.tasks.celery import CeleryTaskQueue
from proseforge.infrastructure.tasks.local import LocalTaskQueue
from proseforge.infrastructure.tasks.memory import InMemoryTaskQueue
from proseforge.runtime.profile import RuntimeProfile, capabilities_for
from proseforge.settings import Settings
from proseforge.workflows.tasks import HANDLERS


def create_task_queue(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> TaskQueue:
    profile = RuntimeProfile(settings.runtime_profile)
    queue_kind = capabilities_for(profile).queue
    if queue_kind == "celery":
        return CeleryTaskQueue()
    if queue_kind == "memory":
        return InMemoryTaskQueue()
    if session_factory is None:
        raise ValueError("native runtime profile requires a session_factory for the local task queue")
    queue = LocalTaskQueue(
        session_factory,
        concurrency=settings.native_worker_concurrency,
        poll_seconds=settings.native_queue_poll_seconds,
    )
    for task_name, handler in HANDLERS.items():
        queue.register(task_name, handler)
    return queue
