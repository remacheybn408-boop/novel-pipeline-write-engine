"""work swarm 写作管线断链修复（B2/B4）回归测试。

sqlite+aiosqlite 真实落库 + 假 provider（无网络、无 PG），种子模式复制自
tests/agents/test_review_target_injection.py。覆盖：
- B2：classify 为 revise/review 且项目无任何章节时，run_entry_response 不建
  agent run，直接落一条中文提示的 assistant 回复；项目有章节时正常建 run；
- B4：尾评审 recheck 任务连续 malformed 到最终 attempt 时降级为 SUCCEEDED +
  “评审不可用”警告 artifact，run 仍 COMPLETED；非 recheck 任务失败语义不变
  （最终 attempt 仍 FAILED，run FAILED）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from proseforge.application.agents.swarm_entry import (
    _EMPTY_PROJECT_REPLY,
    run_entry_response,
)
from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.common.ids import new_id
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import execute_run

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
CHAPTER_CONTENT = "雨夜，主角提着青铜钥匙回城。"


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


class RecordingProvider:
    """按 task_key 定制输出的假 provider（payload 为 str 时原样输出，可模拟非 JSON）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, object] | None = None, usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads or {}
        self._input, self._output = usage
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        user_prompt = "\n".join(str(block.get("text", "")) for block in request.input_blocks)
        self.requests.append({"task_key": str(request.metadata.get("task_key", "")), "user_prompt": user_prompt})
        await asyncio.sleep(0)
        payload = self._payloads.get(str(request.metadata.get("task_key", "")), {"summary": "ok"})
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text=text)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": self._input, "output_tokens": self._output, "total_tokens": self._input + self._output}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


def _patch_provider(monkeypatch, provider: RecordingProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))


async def _seed_project(
    settings: Settings,
    *,
    chapters: list[tuple[int, str, str]] | None = None,
    with_credential: bool = False,
) -> dict[str, object]:
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
            # mode="chat"：关闭叙事 RAG 场景包（避免检索意图调用污染 provider 请求记录）
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Pipeline Fix Test", mode="chat"))
            await uow.session.flush()  # FK parent: chapters -> projects
            for chapter_no, title, content in chapters or []:
                chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=chapter_no, title=title))
                version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
                await uow.chapters.set_active_version(chapter.id, version.id)
            conversation = await uow.conversations.create(Conversation.create(project.id, "Chat"))
            await uow.commit()
            return {"user_id": user.id, "project_id": project.id, "branch_id": conversation.id}
    finally:
        await engine.dispose()


async def _seed_run(
    settings: Settings,
    tasks: list[dict[str, object]],
    *,
    goal: str | None = None,
    chapters: list[tuple[int, str, str]] | None = None,
    budget_limit: int = 1000,
) -> dict[str, object]:
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
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Pipeline Fix Test", mode="chat"))
            await uow.session.flush()  # FK parent: chapters -> projects
            chapter_ids: dict[int, str] = {}
            for chapter_no, title, content in chapters or []:
                chapter = await uow.chapters.add(Chapter.create(project_id=project.id, chapter_no=chapter_no, title=title))
                version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
                await uow.chapters.set_active_version(chapter.id, version.id)
                chapter_ids[chapter_no] = chapter.id
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project.id, goal=goal,
                goal_hash=hashlib.sha256((goal or "").encode()).hexdigest(),
                graph_revision=1, status="PENDING", budget_limit=budget_limit,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()  # FK parent: agent_tasks -> agent_runs
            for item in tasks:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=int(item.get("token_budget", 1)),
                    depends_on=json.dumps(item.get("depends_on", [])),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "project_id": project.id, "chapter_ids": chapter_ids}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    # 只读事务退出时 __aexit__ 会 rollback 并过期实例——必须在会话内快照为 dict
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason")}
            tasks = [
                {key: getattr(task, key) for key in ("task_key", "role", "status", "attempts", "last_error")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            artifacts = [
                {key: getattr(artifact, key) for key in ("id", "task_id", "artifact_type", "payload")}
                for artifact in await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.run_id == run_id))
            ]
            event_types = [
                row.event_type
                for row in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            return run_snapshot, tasks, artifacts, event_types
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B2：空项目的 revise/review 不建 run，直接中文提示回复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["revise", "review"])
async def test_empty_project_revise_review_replies_inline_without_run(executor_settings, intent):
    seeded = await _seed_project(executor_settings)  # 没有任何章节
    queue = RecordingQueue()

    def uow_factory():
        engine_factory = create_engine_and_sessionmaker(executor_settings)
        return SqlAlchemyUnitOfWork(engine_factory[1])

    result = await run_entry_response(
        uow_factory, queue,
        master_key=SecretStr(MASTER_KEY), environment="development",
        branch_id=str(seeded["branch_id"]), content="改写第三章",
        client_request_id=f"req-{uuid.uuid4().hex[:8]}", user_id=str(seeded["user_id"]),
        provider="openai", model="gpt-4.1-mini", intent=intent,
    )

    assert result["intent"] == intent
    assert result["agent_run_id"] is None
    assert result["task_id"] is None
    assert queue.enqueued == []  # 没有建 run，自然没有入队
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            runs = list(await uow.session.scalars(select(AgentRunModel).where(AgentRunModel.project_id == seeded["project_id"])))
            assert runs == []
            assistant = await uow.conversations.get_message(str(result["assistant_message_id"]))
            assert assistant is not None
            assert assistant.status == "COMPLETED"
            assert assistant.content == _EMPTY_PROJECT_REPLY
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_with_chapter_still_creates_revise_run(executor_settings):
    seeded = await _seed_project(executor_settings, chapters=[(3, "第三章", CHAPTER_CONTENT)])
    queue = RecordingQueue()

    def uow_factory():
        engine_factory = create_engine_and_sessionmaker(executor_settings)
        return SqlAlchemyUnitOfWork(engine_factory[1])

    result = await run_entry_response(
        uow_factory, queue,
        master_key=SecretStr(MASTER_KEY), environment="development",
        branch_id=str(seeded["branch_id"]), content="改写第三章",
        client_request_id=f"req-{uuid.uuid4().hex[:8]}", user_id=str(seeded["user_id"]),
        provider="openai", model="gpt-4.1-mini", intent="revise",
    )

    assert result["agent_run_id"] is not None
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, str(result["agent_run_id"]))
            assert run is not None and run.status == "PENDING"
            tasks = list(await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run.id)))
            assert [task.task_key for task in tasks] == ["merge", "rewrite", "recheck"]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B4：recheck 尾评审连续 malformed 降级；其它任务失败语义不变
# ---------------------------------------------------------------------------

_REVISE_GRAPH = [
    {"id": "merge", "role": "merge_editor", "depends_on": []},
    {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"]},
    {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"]},
]


@pytest.mark.asyncio
async def test_recheck_malformed_output_degrades_to_warning_artifact(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={
        "rewrite": {"title": "第三章（终稿）", "content": "改写后的正文：雨夜，主角提着青铜钥匙回城，城门已闭。"},
        # 尾评审连续输出无法解析的文本（无任何 JSON 对象）：3 次 attempt 全部 malformed
        "recheck": "这次评审整体没有发现问题，建议保持现状。",
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        _REVISE_GRAPH,
        goal="改写第三章",
        chapters=[(3, "第三章", CHAPTER_CONTENT)],
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    run, tasks, artifacts, event_types = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    assert status_by_key == {"merge": "SUCCEEDED", "rewrite": "SUCCEEDED", "recheck": "SUCCEEDED"}
    # recheck 用满了重试次数，last_error 记录了解析失败
    recheck_task = next(task for task in tasks if task["task_key"] == "recheck")
    assert "JSONDecodeError" in (recheck_task["last_error"] or "")
    # 终稿复检循环：26 字终稿低于默认 2500 字硬要求 -> 终稿门禁 FAIL ->
    # 第二轮 recheck 再次 malformed（attempts 已满，立即降级）-> 共 4 次调用
    recheck_requests = [request for request in provider.requests if request["task_key"] == "recheck"]
    assert len(recheck_requests) == 4  # 首轮 DEFAULT_MAX_ATTEMPTS + 复检循环第二轮 1 次
    assert "chapter.quality_degraded" in event_types
    # 降级产出“评审不可用”警告 artifact + task.degraded 审计事件
    warning = next(payload for payload in (json.loads(artifact["payload"]) for artifact in artifacts) if payload.get("degraded"))
    assert warning["degraded"] is True
    assert "评审不可用" in warning["summary"]
    assert "task.degraded" in event_types
    # 写作产物（改写终稿）正常交付
    rewritten = next(payload for payload in (json.loads(artifact["payload"]) for artifact in artifacts) if payload.get("rewrite_of"))
    assert rewritten["content"].startswith("改写后的正文")


@pytest.mark.asyncio
async def test_non_recheck_task_malformed_output_still_fails_run(executor_settings, monkeypatch):
    # 降级只对 recheck 开放：评审电池的 malformed 仍然拖垮 run（语义不变）。
    provider = RecordingProvider(payloads={
        "review_continuity": "完全不是 JSON 的输出。",
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(
        executor_settings,
        [{"id": "review_continuity", "role": "continuity_reviewer", "depends_on": []}],
        goal="审校第三章",
        chapters=[(3, "第三章", CHAPTER_CONTENT)],
    )

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "failed"
    run, tasks, _artifacts, event_types = await _read_state(executor_settings, str(seeded["run_id"]))
    assert run["status"] == "FAILED"
    assert all(task["status"] == "FAILED" for task in tasks)
    assert "task.degraded" not in event_types
