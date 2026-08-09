"""create_task_queue 的 profile 分流测试（V15-004）。

native→LocalTaskQueue（注册 5 个任务 handler）、server→CeleryTaskQueue、
test→InMemoryTaskQueue；server 绝不回退 SQLite。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.tasks.celery import CeleryTaskQueue
from proseforge.infrastructure.tasks.factory import create_task_queue
from proseforge.infrastructure.tasks.local import LocalTaskQueue
from proseforge.infrastructure.tasks.memory import InMemoryTaskQueue
from proseforge.settings import Settings

EXPECTED_TASK_NAMES = {
    "proseforge.workflows.generate_novel",
    "proseforge.chat.generate",
    "proseforge.providers.sync_all_models",
    "proseforge.workflows.recover_expired",
    "proseforge.healthcheck",
    "proseforge.agents.execute_run",
    "proseforge.workflows.execute_v2_run",
    "proseforge.retrieval.index_document",
    "proseforge.work.summarize_chapter",
    "proseforge.work.rollup_recap",
}


def _native_settings(db_path: Path) -> Settings:
    return Settings(runtime_profile="native", database_url=f"sqlite:///{db_path.as_posix()}")


@pytest.mark.asyncio
async def test_native_profile_returns_local_queue_with_registered_handlers(tmp_path: Path):
    settings = _native_settings(tmp_path / "queue.db")
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        queue = create_task_queue(settings, session_factory)
        assert isinstance(queue, LocalTaskQueue)
        assert set(queue.registered_task_names) == EXPECTED_TASK_NAMES
    finally:
        await engine.dispose()


def test_server_profile_returns_celery_queue_without_touching_sqlite():
    # server 不创建任何本地引擎：session_factory 给 None 也必须工作。
    settings = Settings(runtime_profile="server")
    queue = create_task_queue(settings, None)
    assert isinstance(queue, CeleryTaskQueue)


def test_server_profile_never_falls_back_to_sqlite():
    # profile/database_url 组合校验在 Settings 层就拒绝，factory 无兜底。
    with pytest.raises(ValueError):
        Settings(runtime_profile="server", database_url="sqlite:///server.db")


def test_test_profile_returns_memory_queue():
    queue = create_task_queue(Settings(runtime_profile="test"), None)
    assert isinstance(queue, InMemoryTaskQueue)


def test_native_profile_requires_session_factory(tmp_path: Path):
    with pytest.raises(ValueError, match="session_factory"):
        create_task_queue(_native_settings(tmp_path / "queue.db"), None)
