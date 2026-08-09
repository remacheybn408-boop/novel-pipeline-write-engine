"""V3 executor（proseforge/workflows/agent_executor.py）宿主可跑测试。

sqlite+aiosqlite 真实落库 + FakeProvider 假模型（无网络、无 PG），
settings/credential 种子模式沿用
tests/integration/workflows/test_generate_novel_context_budget.py。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import (
    DEFAULT_MAX_ATTEMPTS,
    EXECUTOR_VERSION,
    MAX_PARALLEL_TASKS,
    execute_run,
    max_parallel_tasks,
)

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def executor_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'agents.db').as_posix()}"
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


class FakeProvider:
    """记录并发峰值与请求 metadata 的假 provider；可按 task_key 定制输出。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2), delay: float = 0.0):
        self._payloads = payloads or {}
        self._input, self._output = usage
        self._delay = delay
        self.active = 0
        self.peak = 0
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.requests.append(dict(request.metadata))
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            payload = self._payloads.get(request.metadata.get("task_key", ""), {"summary": "ok"})
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            yield GenerationEvent("response.started")
            yield GenerationEvent("content.delta", text=text)
            yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})
        finally:
            self.active -= 1

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


def _patch_provider(monkeypatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _seed_run(settings: Settings, tasks: list[dict[str, object]], *, budget_limit: int = 1000, fault_mode: str | None = None, with_credential: bool = True) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            if with_credential:
                credential_id = f"cred-{uuid.uuid4().hex[:8]}"
                associated = f"{user.id}:openai:{credential_id}".encode()
                encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
                await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            # FK parents: agent_runs.project_id -> projects.id. The project row must be
            # flushed before the run, and the run before its agent_tasks children,
            # because relationship-less tables flush in arbitrary order at commit.
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Agents Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=budget_limit, fault_mode=fault_mode,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    # 只读事务退出时 __aexit__ 会 rollback 并过期实例——必须在会话内快照为 dict
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason", "budget_used", "budget_limit", "event_cursor", "checkpoint_id", "proposal_id", "fault_mode")}
            tasks = [
                {key: getattr(task, key) for key in ("id", "task_key", "role", "status", "attempts", "token_budget", "last_error")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            events = [
                {key: getattr(event, key) for key in ("sequence", "event_type", "payload")}
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            artifacts = [
                {key: getattr(artifact, key) for key in ("id", "task_id", "artifact_type", "sha256", "provenance", "preview", "payload")}
                for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id))
            ]
            return run_snapshot, tasks, events, artifacts
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_execution_dependency_order_and_measured_budget(executor_settings, monkeypatch):
    provider = FakeProvider(usage=(10, 5), delay=0.01)
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "planner", "role": "chief_planner", "token_budget": 10},
        {"id": "scene-a", "role": "scene_writer", "depends_on": ["planner"], "token_budget": 10},
        {"id": "scene-b", "role": "scene_writer", "depends_on": ["planner"], "token_budget": 10},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    assert {task["task_key"] for task in tasks if task["status"] == "SUCCEEDED"} == {"planner", "scene-a", "scene-b"}
    started = {json.loads(event["payload"])["task_key"]: event["sequence"] for event in events if event["event_type"] == "task.started"}
    succeeded = {json.loads(event["payload"])["task_key"]: event["sequence"] for event in events if event["event_type"] == "task.succeeded"}
    # 依赖就绪：planner 成功后两个 scene 才启动
    assert succeeded["planner"] < started["scene-a"]
    assert succeeded["planner"] < started["scene-b"]
    # 有界并行：两个 scene 并发执行且峰值不超上限
    assert provider.peak >= 2
    assert provider.peak <= MAX_PARALLEL_TASKS
    # 实测 usage 结算：3 任务 × (10+5)=45，不是申报的 3×10
    assert run["budget_used"] == 45
    usage_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.usage"]
    assert sum(item["total_tokens"] for item in usage_events) == 45
    assert all(item["input_tokens"] == 10 and item["output_tokens"] == 5 for item in usage_events)
    # 每任务恰好一个 artifact；sha256 为 canonical JSON；provenance 含 task_id/role/model
    assert len(artifacts) == 3
    for artifact in artifacts:
        assert artifact["sha256"] == hashlib.sha256(artifact["payload"].encode()).hexdigest()
        assert artifact["artifact_type"] == "candidate"
        assert {"task_id", "role", "model"} <= set(json.loads(artifact["provenance"]))
        assert len(artifact["preview"]) <= 80
    # run.checkpoint_id 契约：graph 版本 + done 列表 + 事件游标 + 执行器版本
    match = re.fullmatch(r"graph:(\d+)\|done:([^|]*)\|cursor:(\d+)\|exec:(\S+)", run["checkpoint_id"] or "")
    assert match is not None
    assert match.group(1) == "1"
    assert set(match.group(2).split(",")) == {"planner", "scene-a", "scene-b"}
    # cursor 记录最后一次任务提交时的事件游标；终态 run.completed 事件在其后 +1
    assert int(match.group(3)) == run["event_cursor"] - 1
    assert match.group(4) == EXECUTOR_VERSION
    # metadata 携带 role/task_key（mock provider 后续可按角色分支）
    assert all({"role", "task_key"} <= set(request) for request in provider.requests)
    # 新事件类型加入且旧词表完整
    event_types = {event["event_type"] for event in events}
    assert {"run.started", "task.started", "task.lease_acquired", "task.usage", "artifact.committed", "task.succeeded", "run.completed"} <= event_types
    sequences = [event["sequence"] for event in events]
    assert len(set(sequences)) == len(sequences)


@pytest.mark.asyncio
async def test_parallel_claim_respects_semaphore(executor_settings, monkeypatch):
    provider = FakeProvider(usage=(1, 1), delay=0.02)
    _patch_provider(monkeypatch, provider)
    # fixture 用 native profile：认领上限取 profile 并行度（native 4 / server、test 16）
    max_parallel = max_parallel_tasks(executor_settings.runtime_profile)
    seeded = await _seed_run(
        executor_settings,
        [{"id": f"task-{index:02d}", "role": "scene_writer", "token_budget": 1} for index in range(max_parallel + 4)],
        budget_limit=1000,
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, _events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert provider.peak == max_parallel  # 认领上限即并发上限
    assert all(task["status"] == "SUCCEEDED" for task in tasks)
    assert len(artifacts) == max_parallel + 4
    assert run["budget_used"] == 2 * (max_parallel + 4)


@pytest.mark.asyncio
async def test_artifact_schema_rejection_fails_task_but_run_continues(executor_settings, monkeypatch):
    from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult

    async def bad_specialist(_context):
        # scene_writer 的 RolePolicy allowlist 是 {report, candidate}，SceneDraft 必被拒
        return RoleResult(artifact_type="SceneDraft", payload={"title": "只有标题"}, used_tokens=3)

    monkeypatch.setitem(ROLE_HANDLERS, "scene_writer", bad_specialist)
    provider = FakeProvider(usage=(2, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "bad-scene", "role": "scene_writer", "token_budget": 1},
        {"id": "planner", "role": "chief_planner", "token_budget": 1},
        {"id": "follow", "role": "continuity_reviewer", "depends_on": ["planner"], "token_budget": 1},
    ])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    run, tasks, events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    # 校验失败只杀该任务，run 继续其余任务后才以 FAILED 收场
    assert status_by_key == {"bad-scene": "FAILED", "planner": "SUCCEEDED", "follow": "SUCCEEDED"}
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "task(s) failed without retry"
    assert len(artifacts) == 2  # 被拒任务不落 artifact
    failed_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.failed"]
    assert len(failed_events) == 1
    assert "not allowed for role scene_writer" in failed_events[0]["error"]
    assert failed_events[0]["retry"] is False
    assert any(event["event_type"] == "run.failed" for event in events)


@pytest.mark.asyncio
async def test_malformed_output_retries_then_task_fails(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={"fragile": "<<not json>>"}, usage=(1, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "fragile", "role": "chief_planner", "token_budget": 1}])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    run, tasks, events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    task = tasks[0]
    assert task["status"] == "FAILED"
    assert task["attempts"] == DEFAULT_MAX_ATTEMPTS  # 重试耗尽才 FAILED
    assert "JSONDecodeError" in (task["last_error"] or "")
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "task(s) failed without retry"
    assert artifacts == []
    failed_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "task.failed"]
    assert [item["retry"] for item in failed_events] == [True, True, False]


@pytest.mark.asyncio
async def test_fault_provider_timeout_still_raises(executor_settings):
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}], fault_mode="provider_timeout")

    with pytest.raises(TimeoutError):
        await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "TimeoutError"
    assert any(event["event_type"] == "run.failed" for event in events)


@pytest.mark.asyncio
async def test_fault_malformed_json_still_raises(executor_settings):
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}], fault_mode="malformed_json")

    with pytest.raises(json.JSONDecodeError):
        await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "JSONDecodeError"
    assert any(event["event_type"] == "run.failed" for event in events)


@pytest.mark.asyncio
async def test_fault_budget_exhaustion_stays_durable(executor_settings):
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}], fault_mode="budget_exhaustion")

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "budget-exhausted"
    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "BUDGET_EXHAUSTED"
    assert run["terminal_reason"] == "injected budget exhaustion"
    assert any(event["event_type"] == "run.budget_exhausted" for event in events)


@pytest.mark.asyncio
async def test_missing_credential_fails_run_without_crash(executor_settings, monkeypatch):
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}], with_credential=False)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "provider-or-project-not-configured"
    run, tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "provider-or-project-not-configured"
    assert tasks[0]["status"] == "PENDING"  # 任务退回 PENDING，配置后可重试
    assert provider.requests == []  # 未发生模型调用
    assert any(event["event_type"] == "run.failed" for event in events)


async def _seed_cluster_run(settings: Settings) -> dict[str, str]:
    """Two credentialed providers + catalog + global cluster config
    (write=openai/gpt-a, review=deepseek/deep-chat); one write-role task and
    one review-role task."""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            cipher = CredentialCipher(base64.b64decode(MASTER_KEY))
            for provider_id in ("openai", "deepseek"):
                credential_id = f"cred-{provider_id}-{uuid.uuid4().hex[:6]}"
                associated = f"{user.id}:{provider_id}:{credential_id}".encode()
                encrypted = cipher.encrypt(json.dumps({"api_key": f"sk-{provider_id}"}).encode(), associated_data=associated)
                await uow.credentials.create(user.id, provider_id, base64.b64encode(encrypted).decode(), record_id=credential_id)
            from proseforge.domain.ports.model_provider import ProviderModel

            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-a", "GPT A", {}),
                ProviderModel("deepseek", "deep-chat", "Deep Chat", {}),
            ])
            await uow.user_preferences.set(user.id, "cluster", json.dumps({
                "mode": "cluster",
                "write_model": "openai/gpt-a",
                "review_model": "deepseek/deep-chat",
                "revise_model": None,
            }))
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Cluster Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=1000,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            uow.session.add(AgentTaskModel(id=new_id(), run_id=run.id, task_key="planner", role="chief_planner", status="PENDING", token_budget=10, depends_on="[]"))
            uow.session.add(AgentTaskModel(id=new_id(), run_id=run.id, task_key="reviewer", role="continuity_reviewer", status="PENDING", token_budget=10, depends_on=json.dumps(["planner"])))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cluster_mode_resolves_model_per_task_role(executor_settings, monkeypatch):
    from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult

    built: list[str] = []
    seen: list[tuple[str, str, str]] = []  # (task_key, provider_id, model)

    def _fake_build(provider_id, *_args, **_kwargs):
        built.append(provider_id)
        return FakeProvider()

    monkeypatch.setattr("proseforge.providers.factory.build_provider", _fake_build)

    async def _recording_handler(context):
        seen.append((str(context["task"]["task_key"]), str(context["provider_id"]), str(context["model"])))
        return RoleResult(artifact_type="report", payload={"summary": "ok"}, used_tokens=5, input_tokens=3, output_tokens=2)

    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", _recording_handler)
    monkeypatch.setitem(ROLE_HANDLERS, "continuity_reviewer", _recording_handler)
    seeded = await _seed_cluster_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    # One provider instance per credentialed role provider (write/review).
    assert sorted(built) == ["deepseek", "openai"]
    # write role -> write model A; review role -> review model B.
    assert ("planner", "openai", "gpt-a") in seen
    assert ("reviewer", "deepseek", "deep-chat") in seen
    # Artifact provenance records the per-task model, not the run default.
    _run, _tasks, _events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    provenance = {json.loads(artifact["provenance"])["model"] for artifact in artifacts}
    assert provenance == {"gpt-a", "deep-chat"}


async def _seed_reasoning_cluster_run(settings: Settings) -> dict[str, str]:
    """One credentialed provider whose model has a verified reasoning profile
    (opencode-gateway deepseek-v4-flash); a structured task (planner) and a
    prose task (scene_a) on the write seat."""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-openai-{uuid.uuid4().hex[:6]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-openai"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            from proseforge.domain.ports.model_provider import ProviderModel

            await uow.model_catalog.upsert([ProviderModel("openai", "deepseek-v4-flash", "DeepSeek V4 Flash", {})])
            await uow.user_preferences.set(user.id, "cluster", json.dumps({
                "mode": "cluster",
                "write_model": "openai/deepseek-v4-flash",
                "review_model": None,
                "revise_model": None,
            }))
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Reasoning Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=1000,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            uow.session.add(AgentTaskModel(id=new_id(), run_id=run.id, task_key="planner", role="chief_planner", status="PENDING", token_budget=10, depends_on="[]"))
            uow.session.add(AgentTaskModel(id=new_id(), run_id=run.id, task_key="scene_a", role="chief_planner", status="PENDING", token_budget=10, depends_on="[]"))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_elastic_reasoning_flows_per_task_type(executor_settings, monkeypatch):
    """The elastic matrix applies per task type on a verified-profile model:
    the structured task gets the none payload (reasoning_off protection), the
    prose task keeps the write seat's high tier with a max_output reserve —
    and every decision lands in a reasoning.resolved audit event."""
    from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult

    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *_args, **_kwargs: FakeProvider())
    seen: dict[str, dict[str, object]] = {}

    async def _recording_handler(context):
        task = context["task"]
        assert isinstance(task, dict)
        seen[str(task["task_key"])] = {"reasoning": context.get("reasoning"), "max_output_boost": task.get("max_output_boost")}
        return RoleResult(artifact_type="report", payload={"summary": "ok"}, used_tokens=5, input_tokens=3, output_tokens=2)

    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", _recording_handler)
    seeded = await _seed_reasoning_cluster_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    # Structured JSON task: thinking fully off via the profile's none payload.
    assert seen["planner"] == {"reasoning": {"reasoning_effort": "none"}, "max_output_boost": 0}
    # Prose task: the write seat's default high tier applies, with a 50%
    # max_output reserve (base 10 -> boost 5) so thinking cannot eat the draft.
    assert seen["scene_a"] == {"reasoning": {"reasoning_effort": "high"}, "max_output_boost": 5}
    _run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    reasoning_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "reasoning.resolved"]
    levels = {event["task_key"]: event["level"] for event in reasoning_events}
    assert levels == {"planner": "none", "scene_a": "high"}


def _patch_scene_pack(monkeypatch, sections: dict[str, str] | None = None, error: Exception | None = None):
    """Fake NarrativeRetriever.build: returns a fixed pack or raises."""
    from proseforge.application.work.retriever import ScenePack

    calls: list[dict] = []

    async def fake_build(self, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return ScenePack(text="场景包文本" if sections else "", sections=sections or {}, evidence=[], run_id="rr", token_cost=1)

    monkeypatch.setattr("proseforge.application.work.retriever.NarrativeRetriever.build", fake_build)
    return calls


@pytest.mark.asyncio
async def test_scene_pack_built_once_and_injected_into_context(executor_settings, monkeypatch):
    from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult

    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    calls = _patch_scene_pack(monkeypatch, sections={"worldview": "世界观：低魔"})
    seen: list[object] = []

    async def _recording_handler(context):
        seen.append(context.get("scene_pack"))
        return RoleResult(artifact_type="report", payload={"summary": "ok"}, used_tokens=5, input_tokens=3, output_tokens=2)

    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", _recording_handler)
    monkeypatch.setitem(ROLE_HANDLERS, "scene_writer", _recording_handler)
    seeded = await _seed_run(executor_settings, [
        {"id": "planner", "role": "chief_planner", "token_budget": 5},
        {"id": "scene", "role": "scene_writer", "depends_on": ["planner"], "token_budget": 5},
    ])
    # 空索引会跳过检索（rag.skipped_empty_index）：种一章带 active version 才可检索
    await _seed_indexable_chapter(executor_settings, seeded["user_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert seen == [{"worldview": "世界观：低魔"}, {"worldview": "世界观：低魔"}]
    assert len(calls) == 1  # run-level cache: the retriever runs once, not per task
    assert calls[0]["query"] == ""  # seeded run has no goal text


async def _seed_indexable_chapter(settings: Settings, user_id: str) -> None:
    """One chapter with an active version in project-1 (non-empty RAG index)."""
    from proseforge.domain.chapter.entity import Chapter

    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            chapter = await uow.chapters.add(Chapter.create(project_id="project-1", chapter_no=1, title="第一章"))
            version = await uow.chapters.append_version(chapter_id=chapter.id, content="已有正文")
            await uow.chapters.set_active_version(chapter.id, version.id)
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scene_pack_skipped_when_index_empty(executor_settings, monkeypatch):
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    calls = _patch_scene_pack(monkeypatch, sections={"worldview": "世界观：低魔"})
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert calls == []  # 可索引章节为 0：不烧检索
    _run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert any(event["event_type"] == "rag.skipped_empty_index" for event in events)


@pytest.mark.asyncio
async def test_scene_pack_failure_degrades_to_none(executor_settings, monkeypatch):
    from proseforge.application.agents.role_handlers import ROLE_HANDLERS, RoleResult

    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    _patch_scene_pack(monkeypatch, error=RuntimeError("retriever down"))
    seen: list[object] = []

    async def _recording_handler(context):
        seen.append(context.get("scene_pack"))
        return RoleResult(artifact_type="report", payload={"summary": "ok"}, used_tokens=5, input_tokens=3, output_tokens=2)

    monkeypatch.setitem(ROLE_HANDLERS, "chief_planner", _recording_handler)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"  # retriever failure never breaks the run
    assert seen == [None]


@pytest.mark.asyncio
async def test_scene_pack_snapshot_linked_to_swarm_message(executor_settings, monkeypatch):
    """Regression (L12b): a swarm run's retrieval snapshot must attach to the
    placeholder assistant message (conversation/message ids), otherwise the
    cluster output's reference sources stay invisible in the chat UI."""
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    calls = _patch_scene_pack(monkeypatch, sections={"worldview": "世界观：低魔"})
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])
    await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")
    # 空索引会跳过检索（rag.skipped_empty_index）：种一章带 active version 才可检索
    await _seed_indexable_chapter(executor_settings, seeded["user_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert len(calls) == 1
    assert calls[0]["message_id"] == "msg-1"
    assert calls[0]["conversation_id"] == "conv-1"


@pytest.mark.asyncio
async def test_scene_pack_snapshot_stays_null_without_swarm_message(executor_settings, monkeypatch):
    """Headless run (no linked message): the snapshot linkage stays NULL."""
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    calls = _patch_scene_pack(monkeypatch, sections={"worldview": "世界观：低魔"})
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])
    await _seed_indexable_chapter(executor_settings, seeded["user_id"])

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert len(calls) == 1
    assert calls[0]["message_id"] is None
    assert calls[0]["conversation_id"] is None


async def _seed_swarm_message(settings: Settings, run_id: str, project_id: str) -> str:
    """Conversation + branch + placeholder assistant message linked to run."""
    from proseforge.infrastructure.database.models.conversation import (
        ConversationBranchModel,
        ConversationModel,
        MessageModel,
    )

    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            uow.session.add(ConversationModel(id="conv-1", project_id=project_id, title="swarm"))
            await uow.session.flush()
            uow.session.add(ConversationBranchModel(id="branch-1", conversation_id="conv-1", name="Main"))
            await uow.session.flush()
            uow.session.add(MessageModel(id="msg-1", branch_id="branch-1", role="assistant", content="", sequence_no=1, status="PENDING", agent_run_id=run_id))
            await uow.commit()
            return "msg-1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_run_writes_summary_back_to_linked_message(executor_settings, monkeypatch):
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    from proseforge.infrastructure.database.models.conversation import MessageModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            content, status = message.content, message.status
    finally:
        await engine.dispose()
    assert status == "COMPLETED"
    assert "总调度" in content


@pytest.mark.asyncio
async def test_failed_run_writes_failure_summary_back(executor_settings, monkeypatch):
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}], fault_mode="malformed_json")
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")

    with pytest.raises(json.JSONDecodeError):
        # fault injection raises; the outer handler fails the run, writes
        # back, then re-raises for the worker retry chain.
        await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})
    from proseforge.infrastructure.database.models.conversation import MessageModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            content, status = message.content, message.status
    finally:
        await engine.dispose()
    assert status == "FAILED"
    assert "批次未完成" in content and "重试" in content


@pytest.mark.asyncio
async def test_cancelled_run_writes_cancelled_status_back(executor_settings, monkeypatch):
    """Regression: a user-cancelled run must write the linked message back as
    CANCELLED (not FAILED) so the chat UI renders 已取消 without the retry
    button that would 409."""
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}])
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")

    # User cancels before the worker loop picks the run up.
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, seeded["run_id"])
            run.status = "CANCELLED"
            run.terminal_reason = "cancelled-by-user"
            await uow.commit()
    finally:
        await engine.dispose()

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "cancelled"
    from proseforge.infrastructure.database.models.conversation import MessageModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            content, status = message.content, message.status
    finally:
        await engine.dispose()
    assert status == "CANCELLED"
    assert "已取消" in content and "重试" not in content


# ---------------------------------------------------------------------------
# M1 quality gate: review battery -> gate -> SKIPPED revise stage (PASS) or
# auto rewrite (NEEDS_REVISE); chapter writeback + GET run gate field.
# ---------------------------------------------------------------------------

PIPELINE_GRAPH = [
    {"id": "planner", "role": "chief_planner", "token_budget": 5},
    {"id": "character", "role": "character_designer", "depends_on": ["planner"], "token_budget": 5},
    {"id": "scene", "role": "scene_writer", "depends_on": ["character"], "token_budget": 5},
    {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 5},
    {"id": "review_adversarial", "role": "adversarial_reviewer", "depends_on": ["scene"], "token_budget": 5},
    {"id": "review_style", "role": "style_editor", "depends_on": ["scene"], "token_budget": 5},
    {"id": "merge", "role": "merge_editor", "depends_on": ["review_continuity", "review_adversarial", "review_style"], "token_budget": 5},
    {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"], "token_budget": 5},
    {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"], "token_budget": 5},
]

_CLEAN_REVIEW = {"summary": "干净", "findings": []}


async def _read_chapters(settings: Settings, run_id: str):
    from proseforge.infrastructure.database.models.chapter import (
        ChapterModel,
        ChapterVersionModel,
    )

    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            chapter_id = run.chapter_id
            chapters = [
                {"id": chapter.id, "chapter_no": chapter.chapter_no, "title": chapter.title, "active_version_id": chapter.active_version_id}
                for chapter in await uow.session.scalars(select(ChapterModel))
            ]
            versions = [
                {"chapter_id": version.chapter_id, "content": version.content}
                for version in await uow.session.scalars(select(ChapterVersionModel))
            ]
            return chapter_id, chapters, versions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gate_pass_skips_revise_stage_and_writes_chapter_back(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "文" * 2600},
        "review_continuity": _CLEAN_REVIEW,
        "review_adversarial": _CLEAN_REVIEW,
        "review_style": _CLEAN_REVIEW,
    }, usage=(2, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, PIPELINE_GRAPH, budget_limit=10000)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    assert status_by_key["merge"] == status_by_key["rewrite"] == status_by_key["recheck"] == "SKIPPED"
    assert all(status_by_key[key] == "SUCCEEDED" for key in ("planner", "character", "scene", "review_continuity", "review_adversarial", "review_style"))
    # gate.evaluated(passed=True) + 3 条 task.skipped；SKIPPED 计入依赖就绪与终态
    gate_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "gate.evaluated"]
    assert gate_events == [{"passed": True, "reasons": [], "warnings": []}]
    skipped = [json.loads(event["payload"])["task_key"] for event in events if event["event_type"] == "task.skipped"]
    assert sorted(skipped) == ["merge", "recheck", "rewrite"]
    assert any(event["event_type"] == "run.completed" for event in events)
    # 章节回写：scene 产出入章节表，run.chapter_id 落库，真实索引 job 入队
    chapter_id, chapters, versions = await _read_chapters(executor_settings, seeded["run_id"])
    assert chapter_id and chapters[0]["id"] == chapter_id
    assert chapters[0]["chapter_no"] == 1  # 无目标章号：现有章节数 + 1
    active = next(version for version in versions if version["chapter_id"] == chapter_id)
    assert active["content"] == "文" * 2600
    assert any(event["event_type"] == "chapter.written_back" for event in events)
    # GET run 详情 gate 字段 = True
    from types import SimpleNamespace

    from proseforge.api.routes.agent_runs import get_run

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        detail = await get_run(seeded["run_id"], SimpleNamespace(id=seeded["user_id"]), SqlAlchemyUnitOfWork(factory))
    finally:
        await engine.dispose()
    assert detail["gate"] is True
    assert detail["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_gate_needs_revise_runs_rewrite_and_recheck(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={
        "scene": {"title": "回城", "content": "文" * 100},  # 字数不足 2500 → NEEDS_REVISE
        "review_continuity": _CLEAN_REVIEW,
        "review_adversarial": _CLEAN_REVIEW,
        "review_style": _CLEAN_REVIEW,
        "rewrite": {"title": "回城（终稿）", "content": "文" * 3000},
        "recheck": _CLEAN_REVIEW,
    }, usage=(2, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, PIPELINE_GRAPH, budget_limit=10000)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    # 门禁不通过：改写链照常执行，无 SKIPPED
    assert all(status == "SUCCEEDED" for status in status_by_key.values())
    gate_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "gate.evaluated"]
    assert len(gate_events) == 1
    assert gate_events[0]["passed"] is False
    assert gate_events[0]["reasons"] == ["字数不足：100 < 2500"]
    assert not any(event["event_type"] == "task.skipped" for event in events)
    # 改写产物：rewrite_of 指回 scene artifact；章节回写取 rewrite 终稿
    payloads = [json.loads(artifact["payload"]) for artifact in artifacts]
    rewritten = next(payload for payload in payloads if payload.get("rewrite_of"))
    assert rewritten["content"] == "文" * 3000
    chapter_id, _chapters, versions = await _read_chapters(executor_settings, seeded["run_id"])
    active = next(version for version in versions if version["chapter_id"] == chapter_id)
    assert active["content"] == "文" * 3000  # rewrite 优先于 scene
    # GET run 详情 gate 字段 = False
    from types import SimpleNamespace

    from proseforge.api.routes.agent_runs import get_run

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        detail = await get_run(seeded["run_id"], SimpleNamespace(id=seeded["user_id"]), SqlAlchemyUnitOfWork(factory))
    finally:
        await engine.dispose()
    assert detail["gate"] is False


# ---------------------------------------------------------------------------
# Production write graph (intent.py: scene_a/b/c/d parallel drafts -> select
# fusion): the gate must read the select artifact (never "scene missing")
# and the chapter writeback must persist the select/rewrite final draft.
# ---------------------------------------------------------------------------

from proseforge.application.agents.intent import graph_for_intent

PRODUCTION_WRITE_GRAPH = [dict(task, token_budget=5) for task in graph_for_intent("write")]


async def _read_index_jobs(settings: Settings):
    from proseforge.infrastructure.database.models.retrieval import RetrievalJobModel
    from proseforge.infrastructure.tasks.local import TaskJobModel

    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            retrieval_jobs = [
                {key: getattr(job, key) for key in ("id", "job_type", "source_type", "source_id", "status")}
                for job in await uow.session.scalars(select(RetrievalJobModel))
            ]
            queue_jobs = [
                {key: getattr(job, key) for key in ("task_name", "payload_json", "status")}
                for job in await uow.session.scalars(select(TaskJobModel))
            ]
            return retrieval_jobs, queue_jobs
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_production_graph_gate_pass_skips_revise_and_writes_chapter_back(executor_settings, monkeypatch):
    # select 融合调用拿到默认 payload（无 content）→ 回退确定性择优：最长草稿 scene_b 胜出。
    provider = FakeProvider(payloads={
        "scene_a": {"title": "草稿甲", "content": "甲" * 100},
        "scene_b": {"title": "回城", "content": "文" * 2600},
        "scene_c": {"title": "草稿丙", "content": "丙" * 100},
        "review_continuity": _CLEAN_REVIEW,
        "review_adversarial": _CLEAN_REVIEW,
        "review_style": _CLEAN_REVIEW,
    }, usage=(2, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, PRODUCTION_WRITE_GRAPH, budget_limit=10000)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    # 门禁取到 select 草稿并通过：合议与改写链一并 SKIPPED（PASS 不触发不计费）
    assert status_by_key["review_council"] == status_by_key["merge"] == status_by_key["rewrite"] == status_by_key["recheck"] == "SKIPPED"
    assert all(status_by_key[key] == "SUCCEEDED" for key in ("planner", "character", "scene_a", "scene_b", "scene_c", "select", "review_continuity", "review_adversarial", "review_style"))
    gate_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "gate.evaluated"]
    assert gate_events == [{"passed": True, "reasons": [], "warnings": []}]  # 不再恒判 "scene missing"
    # 合议随门禁 PASS 跳过：无 council.committed、无模型调用计费事件
    assert not any(event["event_type"] == "council.committed" for event in events)
    skipped = [json.loads(event["payload"])["task_key"] for event in events if event["event_type"] == "task.skipped"]
    assert sorted(skipped) == ["merge", "recheck", "review_council", "rewrite"]
    # 章节回写：select 终稿入章节表，chapter.written_back 事件产生
    chapter_id, chapters, versions = await _read_chapters(executor_settings, seeded["run_id"])
    assert chapter_id and chapters[0]["id"] == chapter_id
    active = next(version for version in versions if version["chapter_id"] == chapter_id)
    assert active["content"] == "文" * 2600  # select Winner（scene_b 正文）
    written_back = [json.loads(event["payload"]) for event in events if event["event_type"] == "chapter.written_back"]
    assert len(written_back) == 1 and written_back[0]["chapter_id"] == chapter_id
    # 真实索引任务入队：retrieval job + 本地队列 job 双落地
    retrieval_jobs, queue_jobs = await _read_index_jobs(executor_settings)
    index_jobs = [job for job in retrieval_jobs if job["job_type"] == "index_chapter"]
    assert len(index_jobs) == 1
    assert index_jobs[0]["source_type"] == "chapter" and index_jobs[0]["source_id"] == chapter_id
    assert any(job["task_name"] == "proseforge.retrieval.index_document" and json.loads(job["payload_json"])["job_id"] == index_jobs[0]["id"] for job in queue_jobs)


@pytest.mark.asyncio
async def test_production_graph_gate_needs_revise_writes_rewrite_back(executor_settings, monkeypatch):
    provider = FakeProvider(payloads={
        "scene_a": {"title": "草稿甲", "content": "甲" * 50},
        "scene_b": {"title": "回城", "content": "文" * 100},  # Winner 字数不足 2500 → NEEDS_REVISE
        "scene_c": {"title": "草稿丙", "content": "丙" * 50},
        "review_continuity": _CLEAN_REVIEW,
        "review_adversarial": _CLEAN_REVIEW,
        "review_style": _CLEAN_REVIEW,
        "rewrite": {"title": "回城（终稿）", "content": "文" * 3000},
        "recheck": _CLEAN_REVIEW,
    }, usage=(2, 1))
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, PRODUCTION_WRITE_GRAPH, budget_limit=10000)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    assert all(status == "SUCCEEDED" for status in status_by_key.values())  # 无 SKIPPED
    gate_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "gate.evaluated"]
    # 门禁读到的是 select Winner 的字数不足，而非 "scene missing"
    assert gate_events == [{"passed": False, "reasons": ["字数不足：100 < 2500"], "warnings": []}]
    # 章节回写取 rewrite 终稿（优先于 select）
    chapter_id, _chapters, versions = await _read_chapters(executor_settings, seeded["run_id"])
    assert chapter_id
    active = next(version for version in versions if version["chapter_id"] == chapter_id)
    assert active["content"] == "文" * 3000
    assert any(event["event_type"] == "chapter.written_back" for event in events)


# ---------------------------------------------------------------------------
# 中危修复回归：writeback 不翻回 CANCELLED 消息 / 预算耗尽回退已认领任务 /
# 章节号中文数字解析 / 外层 except 不覆盖 CANCELLED/PAUSED。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writeback_never_flips_user_cancelled_message(executor_settings, monkeypatch):
    """回归：消息已被用户置 CANCELLED 时，run 终态 writeback 只写摘要内容，
    绝不把状态翻回 COMPLETED/FAILED。"""
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 5}], fault_mode="malformed_json")
    message_id = await _seed_swarm_message(executor_settings, seeded["run_id"], "project-1")
    # 用户在 writeback 落库前取消了这条消息。
    from proseforge.infrastructure.database.models.conversation import MessageModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            message.status = "CANCELLED"
            await uow.commit()
    finally:
        await engine.dispose()

    with pytest.raises(json.JSONDecodeError):
        await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            content, status = message.content, message.status
    finally:
        await engine.dispose()
    assert status == "CANCELLED"  # 不被 run FAILED 翻回
    assert "批次未完成" in content  # 摘要内容仍写入，便于用户看到失败原因


async def _seed_ordered_run(settings: Settings, tasks: list[dict[str, object]], *, budget_limit: int) -> dict[str, str]:
    """与 _seed_run 相同，但任务 id 由调用方指定（认领顺序按 AgentTaskModel.id）。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"agents-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            credential_id = f"cred-{uuid.uuid4().hex[:8]}"
            associated = f"{user.id}:openai:{credential_id}".encode()
            encrypted = CredentialCipher(base64.b64decode(MASTER_KEY)).encrypt(json.dumps({"api_key": "sk-test"}).encode(), associated_data=associated)
            await uow.credentials.create(user.id, "openai", base64.b64encode(encrypted).decode(), record_id=credential_id)
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Agents Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal_hash="g" * 64,
                graph_revision=1, status="PENDING", budget_limit=budget_limit,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=str(item["task_id"]), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_budget_exhausted_after_lane_recompute(executor_settings, monkeypatch):
    """新语义（两级账本）：首轮认领时车道预算未建，run.budget_limit 重算为
    Σ车道预算×headroom；重算后 budget_used+token_budget 超限仍触发熔断
    BUDGET_EXHAUSTED（防失控），超额任务 FAILED。"""
    provider = FakeProvider(usage=(20000, 20000))  # task-01 一次结算 40000
    _patch_provider(monkeypatch, provider)
    # task-02 依赖 task-01：第一轮 task-01 认领结算 40000 且 limit 重算为
    # int(8192*0.65)*2*1.5=15972；第二轮 task-02 起前估算 40000+5 > 15972 → 熔断
    seeded = await _seed_ordered_run(executor_settings, [
        {"task_id": "task-01", "id": "first", "role": "chief_planner", "token_budget": 1},
        {"task_id": "task-02", "id": "heavy", "role": "chief_planner", "token_budget": 5, "depends_on": ["first"]},
    ], budget_limit=2)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "budget-exhausted"
    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "BUDGET_EXHAUSTED"
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            rows = {task.task_key: (task.status, task.lease_owner) for task in await uow.session.scalars(select(AgentTaskModel))}
    finally:
        await engine.dispose()
    assert rows["first"] == ("SUCCEEDED", None)  # 首轮认领（账本未建不查）正常结算
    assert rows["heavy"] == ("FAILED", None)  # 重算后超额任务 FAILED
    assert any(event["event_type"] == "run.budget_recomputed" for event in events)
    assert any(event["event_type"] == "run.budget_exhausted" for event in events)
    assert len(provider.requests) == 1  # 只有 task-01 发生了模型调用


@pytest.mark.asyncio
async def test_lane_recompute_never_lowers_externally_raised_limit(executor_settings, monkeypatch):
    """BUDGET_EXHAUSTED retry 经 API 上调 budget_limit 后，重跑 run 的首轮
    lane 账本重算绝不把上限降回去（否则脱困 retry 必然在同一任务再熔断）。"""
    provider = FakeProvider(usage=(10, 10))
    _patch_provider(monkeypatch, provider)
    # 车道重算值 = int(8192*0.65)*2*1.5 = 15972；外部上调到 100000 必须保留
    seeded = await _seed_ordered_run(executor_settings, [
        {"task_id": "task-01", "id": "first", "role": "chief_planner", "token_budget": 1},
        {"task_id": "task-02", "id": "second", "role": "chief_planner", "token_budget": 1, "depends_on": ["first"]},
    ], budget_limit=100000)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["budget_limit"] == 100000  # 未被重算降回 15972
    recomputed = next(event for event in events if event["event_type"] == "run.budget_recomputed")
    assert json.loads(str(recomputed["payload"]))["budget_limit"] == 100000


@pytest.mark.asyncio
async def test_writeback_chapter_parses_chinese_chapter_no(executor_settings, monkeypatch):
    """回归：goal「写第三章」必须定位到第 3 章；旧正则只认阿拉伯数字，
    会错位建成第 max+1=2 章。"""
    provider = FakeProvider(payloads={"scene": {"title": "新章", "content": "文" * 100}})
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "scene", "role": "scene_writer", "token_budget": 5}])
    # goal 用中文数字；只种第 1 章：max+1=2，与正确答案 3 区分开
    from proseforge.domain.chapter.entity import Chapter

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, seeded["run_id"])
            run.goal = "写第三章"
            await uow.chapters.add(Chapter.create(project_id="project-1", chapter_no=1, title="第一章"))
            await uow.commit()
    finally:
        await engine.dispose()

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    chapter_id, chapters, _versions = await _read_chapters(executor_settings, seeded["run_id"])
    by_no = {chapter["chapter_no"]: chapter for chapter in chapters}
    assert 3 in by_no and by_no[3]["id"] == chapter_id  # 写回第 3 章而非第 2 章
    _run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    written_back = [json.loads(event["payload"]) for event in events if event["event_type"] == "chapter.written_back"]
    assert written_back[0]["chapter_no"] == 3


@pytest.mark.asyncio
async def test_outer_except_preserves_cancelled_run(executor_settings, monkeypatch):
    """回归：外层兜底 except 不得把已 CANCELLED 的 run 覆盖成 FAILED，
    只补 run.error 审计事件。"""
    provider = FakeProvider()
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "planner", "role": "chief_planner", "token_budget": 1}])

    async def cancel_then_boom(_partials, _max_parallel):
        # 用户取消在 claim 提交后落库，随后并行执行阶段整体崩溃。
        engine, factory = create_engine_and_sessionmaker(executor_settings)
        try:
            async with SqlAlchemyUnitOfWork(factory) as cancel_uow:
                run = await cancel_uow.session.get(AgentRunModel, seeded["run_id"])
                run.status = "CANCELLED"
                run.terminal_reason = "cancelled-by-user"
                await cancel_uow.commit()
        finally:
            await engine.dispose()
        raise RuntimeError("executor boom")

    monkeypatch.setattr("proseforge.workflows.agent_executor.bounded_parallel", cancel_then_boom)

    with pytest.raises(RuntimeError, match="executor boom"):
        await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "CANCELLED"  # 不被兜底覆盖成 FAILED
    assert run["terminal_reason"] == "cancelled-by-user"
    error_events = [json.loads(event["payload"]) for event in events if event["event_type"] == "run.error"]
    assert error_events == [{"preserved_status": "CANCELLED", "reason": "RuntimeError"}]


# ---------------------------------------------------------------------------
# 记忆优先（先查再想）：executor 快照 → 全角色注入 → memory.seen 审计
# ---------------------------------------------------------------------------


class _PromptCaptureProvider(FakeProvider):
    """在 FakeProvider 之上记录每次调用的 user prompt 全文（记忆注入断言用）。"""

    def __init__(self, payloads: dict[str, object] | None = None):
        super().__init__(payloads)
        self.user_prompts: list[str] = []

    async def stream(self, request):
        self.user_prompts.append(str(request.input_blocks[0]["text"]))
        async for event in super().stream(request):
            yield event


@pytest.mark.asyncio
async def test_executor_snapshots_memory_slice_into_task_context(executor_settings, monkeypatch):
    """预置 ACCEPTED 项目级记忆 → 任务提示词先查到记忆 + memory.seen 审计事件。"""
    provider = _PromptCaptureProvider({"scene": {"title": "回城", "content": "雨夜，主角回城。"}})
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [{"id": "scene", "role": "scene_writer", "token_budget": 10}])
    # _seed_run 之后向同一 sqlite 文件预置已批准记忆（项目级作用域）
    from proseforge.application.agents.memory_service import encode_value
    from proseforge.infrastructure.database.models.agents import AgentMemoryModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            uow.session.add(AgentMemoryModel(
                id="mem-1", project_id="project-1", run_id="", memory_key="人物状态·林雪",
                value=encode_value("第1章：情绪：决绝", confidence=0.6, revision=1),
                source_artifact_id="ch-1", status="ACCEPTED",
            ))
            await uow.commit()
    finally:
        await engine.dispose()

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    assert provider.user_prompts  # scene_writer 初稿 + 自我打磨两轮均带记忆
    assert all("人物状态·林雪" in prompt for prompt in provider.user_prompts)
    assert all("第1章：情绪：决绝" in prompt for prompt in provider.user_prompts)
    _run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    seen = [json.loads(event["payload"]) for event in events if event["event_type"] == "memory.seen"]
    assert seen == [{"count": 1}]


@pytest.mark.asyncio
async def test_reviewer_prompt_injects_memory_slice(executor_settings, monkeypatch):
    """专家评审簇同款注入：continuity_reviewer 评审前先看到已批准记忆。"""
    provider = _PromptCaptureProvider({
        "scene": {"title": "回城", "content": "雨夜，主角回城。"},
        "review": {"summary": "ok", "findings": []},
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings, [
        {"id": "scene", "role": "scene_writer", "token_budget": 10},
        {"id": "review", "role": "continuity_reviewer", "depends_on": ["scene"], "token_budget": 10},
    ])
    from proseforge.application.agents.memory_service import encode_value
    from proseforge.infrastructure.database.models.agents import AgentMemoryModel

    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            uow.session.add(AgentMemoryModel(
                id="mem-2", project_id="project-1", run_id="", memory_key="关键道具·戒指",
                value=encode_value("第1章：李雷持有", confidence=0.6, revision=1),
                source_artifact_id="ch-1", status="ACCEPTED",
            ))
            await uow.commit()
    finally:
        await engine.dispose()

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    review_prompts = [prompt for prompt in provider.user_prompts if "评审对象" in prompt]
    assert len(review_prompts) == 1
    assert "关键道具·戒指" in review_prompts[0]
    _run, _tasks, events, _artifacts = await _read_state(executor_settings, seeded["run_id"])
    seen = [json.loads(event["payload"]) for event in events if event["event_type"] == "memory.seen"]
    assert seen == [{"count": 1}, {"count": 1}]  # scene + review 各自记一次
