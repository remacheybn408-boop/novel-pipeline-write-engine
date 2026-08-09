"""批量写作 dispatcher（analyze -> 逐章串行 write run 链）回归测试。

sqlite+aiosqlite 真实落库 + 假 provider（无网络、无 PG），种子模式复制自
tests/agents/test_swarm_pipeline_fixes.py。覆盖：
- analyze run COMPLETED 后读取 analyst artifact 的 chapters，自动落
  batch.planned 事件并立即派发第 1 章 write run（goal 含「写第N章」、
  幂等键 batch:{analyze_run_id}:{N}）；
- 串行链：前一章 run COMPLETED 后才派发下一章；单章 FAILED 自动跳过，
  整批继续，末章结束后落 batch.completed 汇总并回写 analyze 消息；
- 幂等防重：钩子重复触发不重复建 run/入队；
- 护栏：>30 章截断、单章大纲不启动批量、章 run 被取消则整批终止。

测试里 batch_dispatch.graph_for_intent 被替换为单 scene 任务的小图，聚焦
调度逻辑本身（12 任务写管线由 test_agent_executor 等覆盖）。
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

from proseforge.application.agents.batch_dispatch import (
    BATCH_MAX_CHAPTERS,
    book_outline_from_goal,
    chapter_goal,
    chapter_idempotency_key,
    later_chapter_reveals,
    normalize_chapters,
    normalize_volumes,
    on_run_terminal,
    parse_batch_key,
    render_volume_labels,
    requested_chapter_limit,
)
from proseforge.application.agents.quality_gate import (
    parse_min_words,
    parse_required_clues,
)
from proseforge.application.agents.review_target import parse_chapter_no
from proseforge.domain.common.ids import new_id
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.ports.model_provider import GenerationEvent
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.chapter import ChapterModel
from proseforge.infrastructure.database.models.conversation import MessageModel
from proseforge.infrastructure.database.models.plugin import UserBuiltinSkillStateModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.infrastructure.security.credential_cipher import CredentialCipher
from proseforge.settings import Settings, get_settings
from proseforge.workflows.agent_executor import execute_run

MASTER_KEY = base64.b64encode(b"k" * 32).decode()

OUTLINE_GOAL = "# 一、风起\n相遇。\n# 二、云涌\n冲突。\n# 三、惊雷\n决战。"

CHAPTERS_3 = [
    {"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "玉佩来历"},
    {"chapter_no": 2, "title": "云涌", "summary": "冲突"},
    {"chapter_no": 3, "title": "惊雷", "summary": "决战"},
]

# Small stand-in for the 12-task write pipeline: one scene task is enough
# for writeback_chapter (task_key "scene") and keeps the dispatch tests fast.
_SMALL_WRITE_GRAPH = [{"id": "scene", "role": "scene_writer", "depends_on": []}]

SCENE_DRAFT = {"title": "雨夜", "content": "雨夜，主角提着青铜钥匙回城。"}


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


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))


def _patch_dispatch(monkeypatch, provider: RecordingProvider, queue: RecordingQueue) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)
    # Both the dispatcher and the executor's writeback enqueue through the
    # task-queue factory; capture every enqueue on the RecordingQueue.
    monkeypatch.setattr("proseforge.infrastructure.tasks.factory.create_task_queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr("proseforge.application.agents.batch_dispatch.create_task_queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr("proseforge.application.agents.batch_dispatch.graph_for_intent", lambda intent: _SMALL_WRITE_GRAPH)


async def _seed_analyze_run(settings: Settings, *, goal: str = OUTLINE_GOAL) -> dict[str, object]:
    """work 模式项目 + PENDING analyze run（三席位 + analyze_merge 四任务图）+ 关联的
    swarm 占位 assistant 消息（批量汇总回写的落点）。"""
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
            project = await uow.projects.add(Project.create(owner_id=user.id, slug=f"p-{uuid.uuid4().hex[:8]}", title="Batch Dispatch Test", mode="work"))
            # 关闭叙事 RAG 场景包，避免检索意图调用污染 provider 请求记录
            uow.session.add(UserBuiltinSkillStateModel(id=new_id(), user_id=user.id, skill_key="builtin-narrative-rag", enabled=False, created_at=datetime.now(UTC)))
            conversation = await uow.conversations.create(Conversation.create(project.id, "Chat"))
            now = datetime.now(UTC)
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id=project.id, goal=goal,
                goal_hash=hashlib.sha256(goal.encode()).hexdigest(),
                graph_revision=1, status="PENDING", budget_limit=80000,
                created_at=now, updated_at=now,
            )
            uow.session.add(run)
            await uow.session.flush()  # FK parent: agent_tasks -> agent_runs
            # 现行 analyze 图：结构/人物/伏笔三席位并行 -> analyze_merge 融合
            # （depends_on 存 task_key，与 create_agent_run 的落库格式一致）
            seat_keys = ["analyze_structure", "analyze_cast", "analyze_hooks"]
            for seat_key in seat_keys:
                uow.session.add(AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=seat_key, role="analyst",
                    status="PENDING", token_budget=1, depends_on="[]",
                ))
            uow.session.add(AgentTaskModel(
                id=new_id(), run_id=run.id, task_key="analyze_merge", role="merge_editor",
                status="PENDING", token_budget=1, depends_on=json.dumps(seat_keys),
            ))
            message = await uow.conversations.append_message(conversation.id, "assistant", "", None, "PENDING", user_id=user.id)
            await uow.conversations.set_message_agent_run(message.id, run.id)
            await uow.commit()
            return {"run_id": run.id, "user_id": user.id, "project_id": project.id, "message_id": message.id}
    finally:
        await engine.dispose()


async def _runs_for_project(settings: Settings, project_id: str) -> list[dict[str, object]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            return [
                {key: getattr(run, key) for key in ("id", "status", "goal", "idempotency_key", "chapter_id")}
                for run in await uow.session.scalars(select(AgentRunModel).where(AgentRunModel.project_id == project_id))
            ]
    finally:
        await engine.dispose()


async def _event_payloads(settings: Settings, run_id: str) -> list[dict[str, object]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            return [
                {"event_type": row.event_type, "payload": json.loads(row.payload)}
                for row in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence))
            ]
    finally:
        await engine.dispose()


async def _message_snapshot(settings: Settings, message_id: str) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            return {"status": message.status, "content": message.content}
    finally:
        await engine.dispose()


def _analyst_payload(chapters: list[dict[str, object]]) -> dict[str, object]:
    return {"title": "烛龙传", "total_chapters": len(chapters), "chapters": chapters}


def _batch_runs(runs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [run for run in runs if parse_batch_key(str(run["idempotency_key"] or "")) is not None]


# ---------------------------------------------------------------------------
# 章节展开 + 首章立即派发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_completion_plans_batch_and_dispatches_first_chapter(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3)})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    result = await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]})

    assert result == "completed"
    # batch.planned 落在 analyze run 上，3 章全量入计划
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    planned = next(event["payload"] for event in events if event["event_type"] == "batch.planned")
    assert planned["total"] == 3 and planned["truncated"] is False
    assert [chapter["chapter_no"] for chapter in planned["chapters"]] == [1, 2, 3]

    # 第 1 章 write run 已创建并入队；goal 保证 parse_chapter_no 能解析出 1
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    chapter_runs = _batch_runs(runs)
    assert len(chapter_runs) == 1
    first = chapter_runs[0]
    assert first["status"] == "PENDING"
    assert first["idempotency_key"] == chapter_idempotency_key(str(seeded["run_id"]), 1)
    assert "写第1章《风起》" in str(first["goal"])
    assert "本章大纲：相遇" in str(first["goal"])
    assert parse_chapter_no(str(first["goal"])) == 1
    enqueued_run_ids = [str(payload.get("run_id")) for _task, payload in queue.enqueued if str(_task) == "proseforge.agents.execute_run"]
    assert str(first["id"]) in enqueued_run_ids

    # 批量不触碰 analyze 消息的回写总结（章节清单照常）
    message = await _message_snapshot(executor_settings, str(seeded["message_id"]))
    assert message["status"] == "COMPLETED"
    assert "共 3 章工作流" in str(message["content"])


# ---------------------------------------------------------------------------
# 串行链：前一章完成后才派发下一章；全部结束后产出批次汇总
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serial_chain_advances_and_closes_with_summary(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2]), "scene": SCENE_DRAFT})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    # 第 1 章 run 完成 -> 派发第 2 章；任一时刻只有一个在飞章 run
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    first = next(run for run in _batch_runs(runs) if str(run["idempotency_key"]).endswith(":1"))
    assert await execute_run({"run_id": first["id"], "user_id": seeded["user_id"]}) == "completed"

    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    chapter_runs = _batch_runs(runs)
    assert len(chapter_runs) == 2
    status_by_key = {str(run["idempotency_key"]).rsplit(":", 1)[1]: run for run in chapter_runs}
    assert status_by_key["1"]["status"] == "COMPLETED"
    assert status_by_key["2"]["status"] == "PENDING"
    # 第 1 章正文已回写为项目章节（writeback_chapter 链路不变）
    assert status_by_key["1"]["chapter_id"] is not None
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            chapter_no = await uow.session.scalar(select(ChapterModel.chapter_no).where(ChapterModel.id == status_by_key["1"]["chapter_id"]))
            assert chapter_no == 1
    finally:
        await engine.dispose()

    # 第 2 章 run 完成 -> 无下一章：batch.completed 汇总 + analyze 消息追加批次总结
    assert await execute_run({"run_id": status_by_key["2"]["id"], "user_id": seeded["user_id"]}) == "completed"

    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    completed = next(event["payload"] for event in events if event["event_type"] == "batch.completed")
    assert completed["succeeded"] == [1, 2]
    assert completed["skipped"] == []
    message = await _message_snapshot(executor_settings, str(seeded["message_id"]))
    assert "批量写作完成，共 2 章：成功 2 章（第1、2章）。" in str(message["content"])


# ---------------------------------------------------------------------------
# 单章失败跳过，整批不拖垮
# ---------------------------------------------------------------------------


class _FirstChapterFailsProvider(RecordingProvider):
    """前 3 次 scene 调用（第 1 章 run 的全部 attempt）输出非 JSON，之后恢复正常。"""

    def __init__(self) -> None:
        super().__init__(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2]), "scene": SCENE_DRAFT})
        self._scene_calls = 0

    async def stream(self, request):
        if str(request.metadata.get("task_key", "")) == "scene":
            self._scene_calls += 1
            if self._scene_calls <= 3:  # DEFAULT_MAX_ATTEMPTS
                async for event in self._malformed():
                    yield event
                return
        async for event in super().stream(request):
            yield event

    async def _malformed(self):
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text="这不是 JSON 的场景草稿。")
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})


@pytest.mark.asyncio
async def test_failed_chapter_is_skipped_and_batch_still_completes(executor_settings, monkeypatch):
    provider = _FirstChapterFailsProvider()
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    first = next(run for run in _batch_runs(runs) if str(run["idempotency_key"]).endswith(":1"))

    # 第 1 章 run 连续 malformed -> FAILED；钩子跳过它并派发第 2 章
    assert await execute_run({"run_id": first["id"], "user_id": seeded["user_id"]}) == "failed"
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    status_by_key = {str(run["idempotency_key"]).rsplit(":", 1)[1]: run for run in _batch_runs(runs)}
    assert status_by_key["1"]["status"] == "FAILED"
    assert status_by_key["2"]["status"] == "PENDING"

    # 第 2 章完成 -> 批次汇总：成功 [2]，跳过 [1]
    assert await execute_run({"run_id": status_by_key["2"]["id"], "user_id": seeded["user_id"]}) == "completed"
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    completed = next(event["payload"] for event in events if event["event_type"] == "batch.completed")
    assert completed["succeeded"] == [2]
    assert completed["skipped"] == [1]
    message = await _message_snapshot(executor_settings, str(seeded["message_id"]))
    assert "成功 1 章（第2章），跳过 1 章（第1章" in str(message["content"])


# ---------------------------------------------------------------------------
# 写回异常的 COMPLETED 章不计入成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_chapter_without_writeback_counts_as_anomaly(executor_settings, monkeypatch):
    """章 run COMPLETED 但无 chapter.written_back 事件（writeback commit 失败）：
    批次汇总必须把它计入"写回异常"而非成功——正文没落库不能按成功上报。"""
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2]), "scene": SCENE_DRAFT})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    first = next(run for run in _batch_runs(runs) if str(run["idempotency_key"]).endswith(":1"))
    assert await execute_run({"run_id": first["id"], "user_id": seeded["user_id"]}) == "completed"

    # 模拟 writeback commit 失败：chapter.written_back 事件从未落库
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            lost = await uow.session.scalar(
                select(AgentEventModel).where(
                    AgentEventModel.run_id == first["id"], AgentEventModel.event_type == "chapter.written_back"
                )
            )
            assert lost is not None
            await uow.session.delete(lost)
            await uow.session.flush()
            uow.session.add(AgentEventModel(
                id=new_id(), run_id=str(first["id"]), sequence=int(lost.sequence),
                event_type="chapter.writeback_failed", payload=json.dumps({"error": "OperationalError"}, sort_keys=True),
            ))
            await uow.commit()
    finally:
        await engine.dispose()

    # 第 2 章完成 -> 批次汇总：成功 [2]，写回异常 [1]（不是成功，也不是跳过）
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    second = next(run for run in _batch_runs(runs) if str(run["idempotency_key"]).endswith(":2"))
    assert await execute_run({"run_id": second["id"], "user_id": seeded["user_id"]}) == "completed"

    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    completed = next(event["payload"] for event in events if event["event_type"] == "batch.completed")
    assert completed["succeeded"] == [2]
    assert completed["skipped"] == []
    assert completed["writeback_missing"] == [1]
    message = await _message_snapshot(executor_settings, str(seeded["message_id"]))
    assert "成功 1 章（第2章）" in str(message["content"])
    assert "写回异常 1 章（第1章" in str(message["content"])


# ---------------------------------------------------------------------------
# 幂等防重：钩子重复触发不重复派发
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replayed_terminal_hook_does_not_double_dispatch(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2]), "scene": SCENE_DRAFT})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    # analyze 完成钩子连打两次：第 1 章 run 只建一次
    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    await on_run_terminal(
        create_engine_and_sessionmaker(executor_settings)[1], executor_settings,
        run_id=str(seeded["run_id"]), user_id=str(seeded["user_id"]), status="COMPLETED",
    )
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    assert len(_batch_runs(runs)) == 1

    # 第 1 章完成后，其完成钩子连打两次：第 2 章 run 只建一次、只入队一次
    first = _batch_runs(runs)[0]
    assert await execute_run({"run_id": first["id"], "user_id": seeded["user_id"]}) == "completed"
    session_factory = create_engine_and_sessionmaker(executor_settings)[1]
    for _ in range(2):
        await on_run_terminal(
            session_factory, executor_settings,
            run_id=str(first["id"]), user_id=str(seeded["user_id"]), status="COMPLETED",
        )
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    chapter_runs = _batch_runs(runs)
    assert len(chapter_runs) == 2
    second_key = chapter_idempotency_key(str(seeded["run_id"]), 2)
    enqueued_second = [
        payload for _task, payload in queue.enqueued
        if str(_task) == "proseforge.agents.execute_run" and any(str(run["id"]) == str(payload.get("run_id")) and str(run["idempotency_key"]) == second_key for run in chapter_runs)
    ]
    assert len(enqueued_second) == 1


# ---------------------------------------------------------------------------
# 护栏：30 章截断 / 单章不启动 / 章 run 取消终止整批
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_truncated_at_max_chapters(executor_settings, monkeypatch):
    chapters = [{"chapter_no": index, "title": f"第{index}章", "summary": f"大纲{index}"} for index in range(1, BATCH_MAX_CHAPTERS + 6)]
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(chapters)})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    planned = next(event["payload"] for event in events if event["event_type"] == "batch.planned")
    assert planned["total"] == BATCH_MAX_CHAPTERS
    assert planned["truncated"] is True
    assert len(planned["chapters"]) == BATCH_MAX_CHAPTERS


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("大纲\n---\n请严格按照以上大纲，先一口气写完全部前5章正文（第1章到第5章）。", 5),
        ("先写前10章", 10),
        ("一口气写完前三章", 3),
        ("第1章到第7章", 7),
        ("第1-5章", 5),
        ("第3章到第5章", None),  # 起点 >1 不是限量
        ("一口气写完全部13章正文", None),  # 无「前」上限语义
        # 大纲卷目标题不是限量指令（冒号结尾的章节范围须忽略）
        ("## 卷一 · 废土禁忌（第 1-6 章：末世 + 赛博朋克）\n### 第 1 章 雨夜\n---\n请按大纲一口气写完全部 30 章正文。", None),
        ("## 卷一（第1-6章:铺垫阶段）\n请写完全部30章", None),
        ("卷一 前6章：世界观铺垫\n---\n请一口气写完全部30章正文", None),
        # 指令段限量依然生效（与卷标同时出现时认指令、不认卷标）
        ("## 卷一（第 1-6 章：末世）\n---\n请先写前3章", 3),
    ],
)
def test_requested_chapter_limit_parsing(goal: str, expected: int | None):
    assert requested_chapter_limit(goal) == expected


@pytest.mark.asyncio
async def test_batch_respects_user_requested_chapter_limit(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3)})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings, goal=OUTLINE_GOAL + "\n先一口气写完全部前2章正文（第1章到第2章）。")

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    planned = next(event["payload"] for event in events if event["event_type"] == "batch.planned")
    assert planned["total"] == 2
    assert planned["truncated"] is True
    assert [chapter["chapter_no"] for chapter in planned["chapters"]] == [1, 2]


@pytest.mark.asyncio
async def test_single_chapter_outline_stays_on_manual_path(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:1])})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    assert "batch.planned" not in [event["event_type"] for event in events]
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    assert _batch_runs(runs) == []
    enqueued_run_ids = [payload.get("run_id") for task, payload in queue.enqueued if str(task) == "proseforge.agents.execute_run"]
    assert enqueued_run_ids == []


@pytest.mark.asyncio
async def test_broken_analyst_output_starts_no_batch(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": {"title": "无章节"}})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    assert "batch.planned" not in [event["event_type"] for event in events]


@pytest.mark.asyncio
async def test_cancelled_chapter_terminates_batch(executor_settings, monkeypatch):
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3), "scene": SCENE_DRAFT})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    first = _batch_runs(runs)[0]

    # 用户取消第 1 章 run：钩子落 batch.terminated，不再派发第 2 章
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, str(first["id"]))
            run.status = "CANCELLED"
            run.terminal_reason = "cancelled by user"
            await uow.commit()
    finally:
        await engine.dispose()
    await on_run_terminal(
        create_engine_and_sessionmaker(executor_settings)[1], executor_settings,
        run_id=str(first["id"]), user_id=str(seeded["user_id"]), status="CANCELLED",
    )

    events = await _event_payloads(executor_settings, str(seeded["run_id"]))
    terminated = next(event["payload"] for event in events if event["event_type"] == "batch.terminated")
    assert terminated["chapter_no"] == 1
    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    assert len(_batch_runs(runs)) == 1


# ---------------------------------------------------------------------------
# target_words 传递：analyst 产出 -> batch.planned -> 每章 goal 显式目标行
# ---------------------------------------------------------------------------


def test_normalize_chapters_carries_target_words():
    payload = {"chapters": [
        {"chapter_no": 2, "title": "云涌", "summary": "冲突", "target_words": "3000-5000字"},
        {"chapter_no": 1, "title": "风起", "summary": "相遇", "target_words": 2800},
        {"chapter_no": 3, "title": "惊雷", "summary": "决战"},
    ]}
    chapters = normalize_chapters(payload)
    assert [chapter["chapter_no"] for chapter in chapters] == [1, 2, 3]
    assert chapters[0]["target_words"] == 2800
    assert chapters[1]["target_words"] == "3000-5000字"
    assert chapters[2]["target_words"] is None


def test_chapter_goal_includes_word_target_line():
    ranged = chapter_goal({"chapter_no": 3, "title": "惊雷", "summary": "决战", "hooks": "", "target_words": "3000-5000字"})
    assert "目标字数：不少于 3000 字" in ranged  # 区间取下限
    single = chapter_goal({"chapter_no": 4, "title": "尾声", "summary": "", "hooks": "", "target_words": 2800})
    assert "目标字数：不少于 2800 字" in single
    qualified = chapter_goal({"chapter_no": 5, "title": "番外", "summary": "", "hooks": "", "target_words": "约3200字"})
    assert "目标字数：不少于 3200 字" in qualified


def test_chapter_goal_without_target_words_unchanged():
    goal = chapter_goal({"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "玉佩来历"})
    assert goal == "写第1章《风起》\n本章大纲：相遇\n伏笔/钩子：玉佩来历"
    # 无法解析的 target_words 不追加目标行
    for bad_value in ("abc", None, True, 5):
        bad = chapter_goal({"chapter_no": 1, "title": "风起", "summary": "", "hooks": "", "target_words": bad_value})
        assert "目标字数" not in bad


# ---------------------------------------------------------------------------
# 全书上下文注入：章节 goal 追加题材 + 全书大纲，且不污染本章自己的
# 字数/线索契约（quality_gate 的 first-match 解析仍取本章值）
# ---------------------------------------------------------------------------


def test_book_outline_from_goal_strips_trailing_directive():
    goal = "第一章：风起\n第二章：云涌\n\n---\n\n请严格按照以上大纲，一口气写完全部12章正文。"
    assert book_outline_from_goal(goal) == "第一章：风起\n第二章：云涌"


def test_book_outline_from_goal_without_separator_kept_verbatim():
    assert book_outline_from_goal(OUTLINE_GOAL) == OUTLINE_GOAL
    assert book_outline_from_goal("") == ""


def test_chapter_goal_appends_genre_and_book_outline():
    outline = "第一章：风起（目标字数：不少于 4000 字）\n第二章：云涌"
    goal = chapter_goal(
        {"chapter_no": 2, "title": "云涌", "summary": "冲突", "hooks": "玉佩来历", "target_words": 2800},
        book_outline=outline, genre="武侠",
    )
    lines = goal.split("\n")
    # 本章头部四行在前，追加节（题材行 + 全书大纲节）在后
    assert lines[:5] == ["写第2章《云涌》", "本章大纲：冲突", "伏笔/钩子：玉佩来历", "目标字数：不少于 2800 字", "题材：武侠"]
    assert lines[5] == "全书大纲（仅作全局设定与伏笔参照，本章只写「写第2章」指定的内容）："
    assert lines[6:] == outline.split("\n")


def test_chapter_goal_without_genre_omits_genre_line():
    goal = chapter_goal(
        {"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "", "target_words": None},
        book_outline="第一章：风起", genre="",
    )
    assert "题材" not in goal
    assert goal.endswith("全书大纲（仅作全局设定与伏笔参照，本章只写「写第1章」指定的内容）：\n第一章：风起")


def test_injected_goal_gate_parsers_still_resolve_chapter_values():
    # 全书大纲里故意放与本章不同的字数与线索行：parse_min_words /
    # parse_required_clues 都是 re.search 第一匹配，本章行在前必须胜出。
    outline = (
        "第三章：惊雷\n"
        "目标字数：不少于 9999 字\n"
        "伏笔/钩子：回收青铜钥匙\n"
        "第四章：尾声"
    )
    goal = chapter_goal(
        {"chapter_no": 2, "title": "云涌", "summary": "冲突", "hooks": "玉佩来历", "target_words": 2800},
        book_outline=outline, genre="武侠",
    )
    assert parse_min_words(goal) == 2800
    assert parse_required_clues(goal) == ["玉佩来历"]


@pytest.mark.asyncio
async def test_dispatched_chapter_goal_carries_book_outline_and_genre(executor_settings, monkeypatch):
    # 端到端：analyze goal 含尾部指令行 + 项目有题材 -> 派发的章 run goal
    # 注入去指令后的全书大纲与题材行
    from proseforge.infrastructure.database.models.project import ProjectModel

    goal_with_directive = OUTLINE_GOAL + "\n\n---\n\n请严格按照以上大纲，一口气写完全部12章正文。"
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2])})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings, goal=goal_with_directive)
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            project = await uow.session.get(ProjectModel, str(seeded["project_id"]))
            project.genre = "武侠"
            await uow.commit()
    finally:
        await engine.dispose()

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    first = _batch_runs(runs)[0]
    goal = str(first["goal"])
    assert goal.startswith("写第1章《风起》")
    assert "题材：武侠" in goal
    assert "全书大纲（仅作全局设定与伏笔参照" in goal
    assert "# 三、惊雷" in goal  # 全书大纲全文注入，不止本章切片
    assert "请严格按照以上大纲" not in goal  # 尾部指令行已剥离


# ---------------------------------------------------------------------------
# 模型上下文继承：普通模式（single_model）批量章节 run 沿用分析 run 的
# provider/model/single_model；集群批量 single_model 保持空，走集群解析
# ---------------------------------------------------------------------------


async def _set_analyze_model_context(settings: Settings, run_id: str, *, provider: str, model: str, single_model: bool | None) -> None:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.provider = provider
            run.model = model
            run.single_model = single_model
            await uow.commit()
    finally:
        await engine.dispose()


async def _chapter_run_snapshot(settings: Settings, analyze_run_id: str, chapter_no: int) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.scalar(
                select(AgentRunModel).where(AgentRunModel.idempotency_key == chapter_idempotency_key(analyze_run_id, chapter_no))
            )
            return {"provider": run.provider, "model": run.model, "single_model": run.single_model}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_single_model_batch_chapters_inherit_analyze_model(executor_settings, monkeypatch):
    # 普通模式批量：章节 run 继承 provider/model，且 single_model 置位
    # （修复前：章节 run 不传模型上下文，回落默认模型或意外集群化）。
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2])})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)
    await _set_analyze_model_context(executor_settings, str(seeded["run_id"]), provider="openai", model="gpt-4.1-mini", single_model=True)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    snapshot = await _chapter_run_snapshot(executor_settings, str(seeded["run_id"]), 1)
    assert snapshot == {"provider": "openai", "model": "gpt-4.1-mini", "single_model": True}


@pytest.mark.asyncio
async def test_cluster_batch_chapters_keep_cluster_resolution(executor_settings, monkeypatch):
    # 集群批量：分析 run single_model 为空 -> 章节 run 不置 single_model，
    # 模型解析权留在 create_agent_run 的集群配置路径（无配置时请求值透传）。
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(CHAPTERS_3[:2])})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)
    await _set_analyze_model_context(executor_settings, str(seeded["run_id"]), provider="openai", model="gpt-4.1-mini", single_model=None)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    snapshot = await _chapter_run_snapshot(executor_settings, str(seeded["run_id"]), 1)
    assert snapshot["single_model"] is None
    assert snapshot["provider"] == "openai"
    assert snapshot["model"] == "gpt-4.1-mini"


# ---------------------------------------------------------------------------
# scene_writer 连贯性基准：注入上一章 active 版本全文（第 1 章无注入）
# ---------------------------------------------------------------------------


async def _seed_chapter_with_active_version(settings: Settings, *, project_id: str, chapter_no: int, title: str, content: str) -> None:
    from proseforge.domain.chapter.entity import Chapter

    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            chapter = await uow.chapters.add(Chapter.create(project_id=project_id, chapter_no=chapter_no, title=title))
            version = await uow.chapters.append_version(chapter_id=chapter.id, content=content)
            await uow.chapters.set_active_version(chapter.id, version.id)
            await uow.commit()
    finally:
        await engine.dispose()


async def _run_scene_writer_with_db(settings: Settings, *, project_id: str, goal: str) -> str:
    """真实 DB 的 uow_factory 跑默认 handler，返回 scene_writer 的 user prompt。"""
    from proseforge.application.agents.role_handlers import default_role_handler

    engine, factory = create_engine_and_sessionmaker(settings)
    provider = RecordingProvider(payloads={"scene": SCENE_DRAFT})
    try:
        context = {
            "run": {"id": f"run-{uuid.uuid4().hex[:8]}", "goal": goal, "project_id": project_id},
            "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene"},
            "provider": provider,
            "provider_id": "openai",
            "model": "gpt-4.1-mini",
            "artifacts": [],
            "uow_factory": lambda: SqlAlchemyUnitOfWork(factory),
        }
        await default_role_handler(context)
    finally:
        await engine.dispose()
    return provider.requests[0]["user_prompt"]


@pytest.mark.asyncio
async def test_scene_writer_injects_previous_chapter_full_text(executor_settings):
    # 写第 2 章：项目里第 1 章已有 active 版本 -> prompt 注入其全文作连贯性基准
    seeded = await _seed_analyze_run(executor_settings)
    await _seed_chapter_with_active_version(
        executor_settings, project_id=str(seeded["project_id"]), chapter_no=1, title="风起", content="雨夜，主角提着青铜钥匙回城。" * 50,
    )

    prompt = await _run_scene_writer_with_db(executor_settings, project_id=str(seeded["project_id"]), goal="写第2章《云涌》\n本章大纲：冲突")

    assert "【上一章全文·连贯性基准】" in prompt
    assert "第1章《风起》" in prompt
    assert "雨夜，主角提着青铜钥匙回城。" in prompt


@pytest.mark.asyncio
async def test_scene_writer_first_chapter_has_no_previous_injection(executor_settings):
    # 写第 1 章：无更前章节（chapter_no=1 的 active 版本不算"上一章"）-> 不注入
    seeded = await _seed_analyze_run(executor_settings)
    await _seed_chapter_with_active_version(
        executor_settings, project_id=str(seeded["project_id"]), chapter_no=1, title="风起", content="雨夜，主角提着青铜钥匙回城。",
    )

    prompt = await _run_scene_writer_with_db(executor_settings, project_id=str(seeded["project_id"]), goal="写第1章《风起》\n本章大纲：相遇")

    assert "【上一章全文·连贯性基准】" not in prompt


# ---------------------------------------------------------------------------
# 禁止明说：后章揭示信息（谜底/真名/身份）确定性推导，注入本章 goal 事前
# 防泄底；清单为空则不输出该行，且不干扰 quality_gate 的本章契约解析
# ---------------------------------------------------------------------------


REVEAL_CHAPTERS = [
    {"chapter_no": 1, "title": "风起", "summary": "相遇", "hooks": "埋入玉佩来历"},
    {"chapter_no": 2, "title": "云涌", "summary": "冲突", "hooks": "揭示魔尊真名玄烬"},
    {"chapter_no": 3, "title": "惊雷", "summary": "决战，黑衣人就是凶手", "hooks": "暂无"},
]


def test_later_chapter_reveals_extracts_hook_and_summary_patterns():
    chapters = normalize_chapters({"chapters": REVEAL_CHAPTERS})
    reveals = later_chapter_reveals(chapters, 1)
    # hooks 行「揭示魔尊真名玄烬」+ summary「黑衣人就是凶手」均被提取
    assert any("玄烬" in reveal for reveal in reveals)
    assert "黑衣人" in reveals
    # 末章无后续章节：清单为空
    assert later_chapter_reveals(chapters, 3) == []


def test_later_chapter_reveals_empty_without_reveal_keywords():
    chapters = normalize_chapters({"chapters": CHAPTERS_3})
    assert later_chapter_reveals(chapters, 1) == []


def test_chapter_goal_forbidden_line_before_book_outline():
    chapters = normalize_chapters({"chapters": REVEAL_CHAPTERS})
    forbidden = later_chapter_reveals(chapters, 1)
    goal = chapter_goal(chapters[0], book_outline="第一章：风起\n第二章：云涌", genre="武侠", forbidden=forbidden)
    lines = goal.split("\n")
    forbidden_line = next(line for line in lines if line.startswith("禁止明说："))
    assert "玄烬" in forbidden_line
    assert "本章只可侧面暗示，不可直接写出" in forbidden_line
    # 全书大纲段仍在，且禁止明说行在它之前
    outline_index = next(index for index, line in enumerate(lines) if line.startswith("全书大纲"))
    assert lines.index(forbidden_line) < outline_index


def test_chapter_goal_no_forbidden_line_without_reveals():
    chapters = normalize_chapters({"chapters": CHAPTERS_3})
    goal = chapter_goal(chapters[0], book_outline="第一章：风起", forbidden=later_chapter_reveals(chapters, 1))
    assert "禁止明说" not in goal
    assert "全书大纲" in goal


@pytest.mark.asyncio
async def test_dispatched_chapter_goal_carries_forbidden_reveals(executor_settings, monkeypatch):
    # 端到端：第 2 章 hooks 含「揭示魔尊真名玄烬」-> 派发的第 1 章 goal
    # 带「禁止明说」行；该行不干扰本章字数/线索契约解析
    provider = RecordingProvider(payloads={"analyze_merge": _analyst_payload(REVEAL_CHAPTERS[:2])})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    goal = str(_batch_runs(runs)[0]["goal"])
    lines = goal.split("\n")
    forbidden_index = next(index for index, line in enumerate(lines) if line.startswith("禁止明说："))
    assert "玄烬" in lines[forbidden_index]
    outline_index = next(index for index, line in enumerate(lines) if line.startswith("全书大纲"))
    assert forbidden_index < outline_index
    assert parse_required_clues(goal) == ["玉佩来历"]


# ---------------------------------------------------------------------------
# 卷一等公民：analyst volumes → goal 标签 → writeback volume_no → rollup 边界
# ---------------------------------------------------------------------------


def test_normalize_volumes_variants():
    payload = {"volumes": [
        {"volume_no": 2, "title": "承", "chapter_range": "11-20"},
        {"volume_no": 1, "title": "起", "chapter_range": [1, 10]},
        {"volume_no": 3, "title": "转", "chapter_range": {"start": 21, "end": 30}},
        {"volume_no": 4, "title": "坏", "chapter_range": "abc"},  # 非法丢弃
        "not-a-dict",  # 非 dict 丢弃
    ]}
    volumes = normalize_volumes(payload)
    assert [(volume["volume_no"], volume["start"], volume["end"]) for volume in volumes] == [(1, 1, 10), (2, 11, 20), (3, 21, 30)]
    assert volumes[0]["title"] == "起"
    assert normalize_volumes({}) == []
    assert normalize_volumes({"volumes": "nope"}) == []


def test_render_volume_labels_round_trips_through_span_parser():
    # 渲染格式必须被 rollup_recap._VOLUME_LABEL_PATTERN 解析回来（闭环契约）
    from proseforge.application.work.rollup_recap import parse_volume_spans

    labels = render_volume_labels([
        {"volume_no": 1, "title": "起", "start": 1, "end": 10},
        {"volume_no": 2, "title": "", "start": 11, "end": 20},
    ])
    assert "卷1（第 1-10 章：起）" in labels
    assert "卷2（第 11-20 章）" in labels
    assert parse_volume_spans(labels) == [(1, 10), (11, 20)]
    assert render_volume_labels([]) == ""


@pytest.mark.asyncio
async def test_analyst_volumes_reach_chapter_goal_and_chapter_row(executor_settings, monkeypatch):
    # 端到端：analyst 产出 volumes -> 派发章 run goal 带「卷结构」标签节 ->
    # 写完写回时 chapters.volume_no 落库（迁移 0052）
    payload = _analyst_payload(CHAPTERS_3[:2])
    payload["volumes"] = [{"volume_no": 1, "title": "起", "chapter_range": "1-2"}]
    provider = RecordingProvider(payloads={"analyze_merge": payload})
    queue = RecordingQueue()
    _patch_dispatch(monkeypatch, provider, queue)
    seeded = await _seed_analyze_run(executor_settings)

    assert await execute_run({"run_id": seeded["run_id"], "user_id": seeded["user_id"]}) == "completed"

    runs = await _runs_for_project(executor_settings, str(seeded["project_id"]))
    goal = str(_batch_runs(runs)[0]["goal"])
    assert "卷结构：" in goal
    assert "卷1（第 1-2 章：起）" in goal

    from proseforge.infrastructure.database.models.agents import (
        AgentArtifactModel,
        AgentRunModel,
        AgentTaskModel,
    )
    from proseforge.workflows.agent_executor import writeback_chapter_for_run

    chapter_run_id = str(_batch_runs(runs)[0]["id"])
    engine, factory = create_engine_and_sessionmaker(executor_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            now = datetime.now(UTC)
            task_id = new_id()
            uow.session.add(AgentTaskModel(
                id=task_id, run_id=chapter_run_id, task_key="select", role="merge_editor",
                status="SUCCEEDED", token_budget=1, depends_on="[]",
            ))
            uow.session.add(AgentArtifactModel(
                id=new_id(), run_id=chapter_run_id, task_id=task_id, artifact_type="candidate",
                sha256="s" * 64, provenance="{}", preview="p",
                payload=json.dumps({"title": "风起", "content": "正文" * 100}, ensure_ascii=False),
            ))
            run_row = await uow.session.get(AgentRunModel, chapter_run_id)
            run_row.status = "COMPLETED"
            run_row.updated_at = now
            await uow.commit()
        written = await writeback_chapter_for_run(factory, executor_settings, run_id=chapter_run_id, user_id=seeded["user_id"])
        assert written is True
        async with SqlAlchemyUnitOfWork(factory) as uow:
            chapter = await uow.session.scalar(select(ChapterModel).where(ChapterModel.project_id == str(seeded["project_id"])))
            assert chapter is not None
            assert chapter.volume_no == 1
    finally:
        await engine.dispose()
