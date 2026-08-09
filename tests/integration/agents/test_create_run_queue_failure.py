"""create_agent_run enqueue 失败回归（真 sqlite）。

run 行 commit 之后 queue.enqueue 抛错：run 会被标 FAILED（queue unavailable），
但关联的 swarm 占位 assistant 消息曾永久卡 PENDING——无任务、无事件。修复后
enqueue 失败路径在同一事务里把占位消息翻 FAILED。
"""

from __future__ import annotations

import base64
import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from proseforge.application.agents.create_run import (
    QueueUnavailableError,
    RunTaskSpec,
    create_agent_run,
)
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


class FailingQueue:
    async def enqueue(self, task_name, payload):
        raise RuntimeError("broker unreachable")


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        runtime_profile="native", master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"), backup_root=str(tmp_path / "backups"),
    )


@pytest.mark.asyncio
async def test_enqueue_failure_fails_run_and_placeholder_message(settings):
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"writer-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            project = Project.create(owner_id=user.id, slug=f"proj-{uuid.uuid4().hex[:8]}", title="Novel", mode="work")
            await uow.projects.add(project)
            conversation = Conversation.create(project.id, "Chat")
            main = await uow.conversations.create(conversation)
            await uow.conversations.append_message(main.id, "user", "写第三章")
            assistant = await uow.conversations.append_message(main.id, "assistant", "", None, "PENDING")
            await uow.commit()

            with pytest.raises(QueueUnavailableError):
                await create_agent_run(
                    uow, FailingQueue(),
                    user_id=user.id, project_id=project.id, goal="写第三章",
                    tasks=[RunTaskSpec(id="planner", role="chief_planner")],
                    budget_limit=100, master_key=SecretStr(MASTER_KEY),
                    assistant_message_id=assistant.id,
                )
            run_id = (await uow.conversations.get_message(assistant.id)).agent_run_id

        # 新会话回读：run FAILED + 占位消息 FAILED + run.queue_failed 事件落库。
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.scalar(select(AgentRunModel).where(AgentRunModel.id == run_id))
            assert run is not None and run.status == "FAILED"
            assert run.terminal_reason == "queue unavailable"
            message = await uow.conversations.get_message(assistant.id)
            assert message is not None and message.status == "FAILED"
            event_types = [
                row.event_type for row in (await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id))).all()
            ]
            assert "run.queue_failed" in event_types
    finally:
        await engine.dispose()
