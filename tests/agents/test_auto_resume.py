"""自动恢复探测（proseforge/application/agents/auto_resume.py）测试。

sqlite+aiosqlite 真实落库，种子模式沿用 tests/agents/test_agent_message_sweeper.py：
- 探测成功 → run 回 RUNNING + run.resumed(probe=true) + 重新入队 + 聊天提醒；
- 探测失败 → 只落 run.resume_probe 事件，run 保持 PAUSED 不入队；
- 已有 2 次 run.resume_probe → 转纯手动，不再探测；
- 人工 PAUSED（无 run.auto_paused 事件）→ 绝不动。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.agents.auto_resume import probe_auto_paused_runs
from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationModel,
    MessageModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def prober_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'auto_resume.db').as_posix()}"
    monkeypatch.setenv("PROSEFORGE_DATABASE_URL", database_url)
    monkeypatch.setenv("PROSEFORGE_RUNTIME_PROFILE", "native")
    monkeypatch.setenv("PROSEFORGE_MASTER_KEY", MASTER_KEY)
    get_settings.cache_clear()
    yield Settings(
        database_url=database_url,
        runtime_profile="native",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    get_settings.cache_clear()


class FakeProbeProvider:
    """ModelProvider stub: stream raises the configured error, else yields
    one completed event. calls counts probe attempts."""

    provider_id = "openai"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def validate_credentials(self) -> dict[str, object]:
        return {"valid": True}

    async def list_models(self) -> list[object]:
        return []

    async def count_tokens(self, request) -> int:
        return 1

    async def stream(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        yield GenerationEvent(event="completed", text="ok")


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, task_name: str, payload: dict[str, object]) -> str:
        self.enqueued.append((task_name, payload))
        return "fake-task-id"


async def _seed(
    settings: Settings,
    *,
    auto_paused: bool = True,
    prior_probes: int = 0,
    run_status: str = "PAUSED",
) -> dict[str, str]:
    """一个 PAUSED run（openai 凭据 + 占位聊天消息）+ 可选的
    run.auto_paused / run.resume_probe 历史事件。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"resume-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Auto Resume Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status=run_status, budget_limit=1000,
                provider="openai", model="gpt-4.1-mini",
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            sequence = 0
            if auto_paused:
                sequence += 1
                uow.session.add(AgentEventModel(
                    id=new_id(), run_id=run.id, sequence=sequence, event_type="run.auto_paused",
                    payload=json.dumps({"streak": 3, "provider": "openai", "model": "gpt-4.1-mini", "error": "boom"}, sort_keys=True),
                ))
            for probe_no in range(1, prior_probes + 1):
                sequence += 1
                uow.session.add(AgentEventModel(
                    id=new_id(), run_id=run.id, sequence=sequence, event_type="run.resume_probe",
                    payload=json.dumps({"probe": probe_no, "error": "Timeout"}, sort_keys=True),
                ))
            run.event_cursor = sequence
            uow.session.add(ConversationModel(id="conv-1", project_id="project-1", title="swarm"))
            await uow.session.flush()
            uow.session.add(ConversationBranchModel(id="branch-1", conversation_id="conv-1", name="Main"))
            await uow.session.flush()
            uow.session.add(MessageModel(id="msg-1", branch_id="branch-1", role="assistant", content="", sequence_no=1, status="COMPLETED", agent_run_id=run.id))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "message_id": "msg-1"}
    finally:
        await engine.dispose()


async def _probe(settings: Settings, queue: FakeQueue, **kwargs) -> int:
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await probe_auto_paused_runs(session_factory, settings, queue, **kwargs)
    finally:
        await engine.dispose()


async def _read_run(settings: Settings, run_id: str) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            events = [
                (event.event_type, json.loads(event.payload))
                for event in await uow.session.scalars(
                    select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence)
                )
            ]
            return {"status": run.status, "events": events}
    finally:
        await engine.dispose()


async def _read_message(settings: Settings, message_id: str) -> str:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            return str(message.content)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_probe_success_resumes_reenqueues_and_notifies(prober_settings, monkeypatch):
    """探测成功 → run 回 RUNNING、落 run.resumed(probe=true)、重新入队
    execute_run，聊天消息追加「总调度：模型已恢复，自动继续写作。」。"""
    provider = FakeProbeProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed(prober_settings)
    queue = FakeQueue()

    probed = await _probe(prober_settings, queue)

    assert probed == 1
    assert provider.calls == 1
    run = await _read_run(prober_settings, seeded["run_id"])
    assert run["status"] == "RUNNING"
    assert ("run.resumed", {"probe": True}) in run["events"]
    assert queue.enqueued == [("proseforge.agents.execute_run", {"run_id": seeded["run_id"], "user_id": seeded["user_id"]})]
    message = await _read_message(prober_settings, seeded["message_id"])
    assert "总调度：模型已恢复，自动继续写作。" in message


@pytest.mark.asyncio
async def test_probe_failure_records_probe_event_only(prober_settings, monkeypatch):
    """探测失败 → 只落 run.resume_probe（含错误类型），run 保持 PAUSED，
    不入队、不打扰聊天消息。"""
    provider = FakeProbeProvider(error=TimeoutError("still down"))
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed(prober_settings)
    queue = FakeQueue()

    probed = await _probe(prober_settings, queue)

    assert probed == 1
    run = await _read_run(prober_settings, seeded["run_id"])
    assert run["status"] == "PAUSED"
    assert ("run.resume_probe", {"error": "TimeoutError", "probe": 1}) in run["events"]
    assert queue.enqueued == []
    message = await _read_message(prober_settings, seeded["message_id"])
    assert message == ""


@pytest.mark.asyncio
async def test_max_probes_reached_switches_to_manual(prober_settings, monkeypatch):
    """已失败 2 次（MAX_RESUME_PROBES）→ 转纯手动：不再探测、不落新事件。"""
    provider = FakeProbeProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed(prober_settings, prior_probes=2)
    queue = FakeQueue()

    probed = await _probe(prober_settings, queue)

    assert probed == 0
    assert provider.calls == 0
    run = await _read_run(prober_settings, seeded["run_id"])
    assert run["status"] == "PAUSED"
    assert [event_type for event_type, _payload in run["events"]].count("run.resume_probe") == 2
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_manual_pause_is_never_probed(prober_settings, monkeypatch):
    """人工暂停（无 run.auto_paused 事件）不在探测范围。"""
    provider = FakeProbeProvider()
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    seeded = await _seed(prober_settings, auto_paused=False)
    queue = FakeQueue()

    probed = await _probe(prober_settings, queue)

    assert probed == 0
    assert provider.calls == 0
    run = await _read_run(prober_settings, seeded["run_id"])
    assert run["status"] == "PAUSED"
    assert run["events"] == []
    assert queue.enqueued == []
