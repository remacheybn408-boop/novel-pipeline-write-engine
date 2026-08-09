"""终稿复检 + 有界改写循环（质量优先）回归测试。

sqlite+aiosqlite 真实落库 + 假 provider（无网络、无 PG），种子模式复制自
tests/agents/test_swarm_pipeline_fixes.py。覆盖：
- 终稿门禁首轮 FAIL -> 自动第二轮改写（改写基底=上一轮终稿，prompt 带门禁原因）-> PASS -> 入库；
- 两轮都 FAIL -> chapter.quality_degraded 事件 + 终稿仍入库（带病标记，不跳章）；
- 初检 PASS -> revise stage 跳过、终稿门禁不触发（既有行为不回归）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.domain.common.ids import new_id
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
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

GOAL = "写第1章《风起》\n本章大纲：雨夜回城\n目标字数：不少于 500 字"

# 简化写作管线：scene -> review -> merge -> rewrite -> recheck
_WRITE_GRAPH = [
    {"id": "scene", "role": "scene_writer", "depends_on": []},
    {"id": "review_continuity", "role": "continuity_reviewer", "depends_on": ["scene"]},
    {"id": "merge", "role": "merge_editor", "depends_on": ["review_continuity"]},
    {"id": "rewrite", "role": "chief_editor", "depends_on": ["merge"]},
    {"id": "recheck", "role": "continuity_reviewer", "depends_on": ["rewrite"]},
]


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


class SequentialProvider:
    """按 task_key 顺序弹出 payload 的假 provider（同 key 多次调用依序取稿）。"""

    provider_id = "fake"

    def __init__(self, payloads: dict[str, list[object]], usage: tuple[int, int] = (4, 2)):
        self._payloads = payloads
        self._input, self._output = usage
        self._calls: dict[str, int] = {}
        self.requests: list[dict[str, str]] = []

    async def stream(self, request):
        key = str(request.metadata.get("task_key", ""))
        user_prompt = "\n".join(str(block.get("text", "")) for block in request.input_blocks)
        self.requests.append({"task_key": key, "user_prompt": user_prompt})
        await asyncio.sleep(0)
        index = self._calls.get(key, 0)
        self._calls[key] = index + 1
        options = self._payloads.get(key, [{"summary": "ok"}])
        payload = options[min(index, len(options) - 1)]
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


def _patch_provider(monkeypatch, provider: SequentialProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


async def _seed_run(settings: Settings, *, goal: str = GOAL, budget_limit: int = 100000) -> dict[str, str]:
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
            # mode="chat"：关闭叙事 RAG 场景包（避免检索意图调用污染 provider 请求记录）
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Final Gate Test", mode="chat"))
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project.id, goal=goal,
                goal_hash=hashlib.sha256(goal.encode()).hexdigest(),
                graph_revision=1, status="PENDING", budget_limit=budget_limit,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()  # FK parent: agent_tasks -> agent_runs
            for item in _WRITE_GRAPH:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(item["id"]), role=str(item["role"]),
                    status="PENDING", token_budget=10, depends_on=json.dumps(item["depends_on"]),
                ))
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "project_id": project.id}
    finally:
        await engine.dispose()


async def _read_state(settings: Settings, run_id: str):
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_snapshot = {key: getattr(run, key) for key in ("id", "status", "terminal_reason", "chapter_id")}
            tasks = [
                {key: getattr(task, key) for key in ("task_key", "status", "attempts")}
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id).order_by(AgentTaskModel.id))
            ]
            events = [
                {"event_type": row.event_type, "payload": json.loads(row.payload)}
                for row in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
            chapter_content = None
            if run.chapter_id:
                from proseforge.infrastructure.database.models.chapter import (
                    ChapterModel,
                    ChapterVersionModel,
                )

                chapter = await uow.session.get(ChapterModel, run.chapter_id)
                version = await uow.session.get(ChapterVersionModel, chapter.active_version_id)
                chapter_content = version.content
            return run_snapshot, tasks, events, chapter_content
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 终稿门禁 FAIL -> 第二轮改写 -> PASS -> 入库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_gate_fail_triggers_second_round_then_passes(executor_settings, monkeypatch):
    provider = SequentialProvider({
        # scene：初稿 + 自我打磨（同一短稿，字数不足触发初检 FAIL）
        "scene": [{"title": "风起", "content": "短稿" * 100}],
        "review_continuity": [{"summary": "无高危", "issues": []}],
        # rewrite：首轮 300 字 -> 扩写重试 400 字（仍不足）；第二轮 600 字达标
        "rewrite": [
            {"title": "改写一", "content": "一" * 300},
            {"title": "改写一扩", "content": "二" * 400},
            {"title": "改写二", "content": "三" * 600},
        ],
        "recheck": [{"summary": "复审通过", "issues": []}],
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    run, _tasks, events, chapter_content = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    event_types = [event["event_type"] for event in events]
    # 初检 FAIL（字数不足）->  revise 阶段 -> 终稿门禁首轮 FAIL -> 第二轮 -> PASS
    assert "gate.evaluated" in event_types
    recheck_failed = next(event["payload"] for event in events if event["event_type"] == "gate.recheck_failed")
    assert recheck_failed["round"] == 1
    assert any("字数不足" in reason for reason in recheck_failed["reasons"])
    final_passed = next(event["payload"] for event in events if event["event_type"] == "gate.final_passed")
    assert final_passed["round"] == 2
    assert "chapter.quality_degraded" not in event_types
    # 第二轮改写：prompt 带终稿门禁原因，改写基底是上一轮终稿（"二"*400）
    rewrite_prompts = [request["user_prompt"] for request in provider.requests if request["task_key"] == "rewrite"]
    assert len(rewrite_prompts) == 3  # 首轮 + 扩写重试 + 第二轮
    assert "质量门禁未通过原因" in rewrite_prompts[2]
    assert "字数不足" in rewrite_prompts[2]
    assert "二" * 100 in rewrite_prompts[2]  # 第二轮基于第一轮终稿继续改
    # 入库的是第二轮达标终稿
    assert chapter_content == "三" * 600


# ---------------------------------------------------------------------------
# 两轮改写都 FAIL -> chapter.quality_degraded + 带病入库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_failed_rounds_ship_with_quality_degraded(executor_settings, monkeypatch):
    provider = SequentialProvider({
        "scene": [{"title": "风起", "content": "短稿" * 100}],
        "review_continuity": [{"summary": "无高危", "issues": []}],
        "rewrite": [
            {"title": "改写一", "content": "一" * 300},
            {"title": "改写一扩", "content": "二" * 350},
            {"title": "改写二", "content": "三" * 300},
            {"title": "改写二扩", "content": "四" * 360},
        ],
        "recheck": [{"summary": "复审通过", "issues": []}],
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    run, _tasks, events, chapter_content = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    event_types = [event["event_type"] for event in events]
    degraded = next(event["payload"] for event in events if event["event_type"] == "chapter.quality_degraded")
    assert degraded["rounds"] == 2
    assert any("字数不足" in reason for reason in degraded["reasons"])
    assert "gate.final_passed" not in event_types
    assert event_types.count("gate.recheck_failed") == 1  # 只重排了一轮
    # 带病入库：最新一轮终稿照常回写，不跳章
    assert chapter_content == "四" * 360


# ---------------------------------------------------------------------------
# 初检 PASS -> revise stage 跳过，终稿门禁不触发（既有行为不回归）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_gate_pass_skips_revise_and_final_gate(executor_settings, monkeypatch):
    provider = SequentialProvider({
        "scene": [{"title": "风起", "content": "合" * 600}],
        "review_continuity": [{"summary": "无高危", "issues": []}],
    })
    _patch_provider(monkeypatch, provider)
    seeded = await _seed_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    run, tasks, events, chapter_content = await _read_state(executor_settings, seeded["run_id"])
    assert run["status"] == "COMPLETED"
    status_by_key = {task["task_key"]: task["status"] for task in tasks}
    assert status_by_key["merge"] == "SKIPPED"
    assert status_by_key["rewrite"] == "SKIPPED"
    assert status_by_key["recheck"] == "SKIPPED"
    event_types = [event["event_type"] for event in events]
    assert "gate.final_passed" not in event_types
    assert "gate.recheck_failed" not in event_types
    assert "chapter.quality_degraded" not in event_types
    # 改写/复审没有发生（rewrite/recheck 零调用），入库的是写作稿
    assert not any(request["task_key"] in {"rewrite", "recheck"} for request in provider.requests)
    assert chapter_content == "合" * 600
