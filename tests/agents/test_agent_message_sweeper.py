"""消息级 sweeper（proseforge/application/messages/sweeper.py）测试。

sqlite+aiosqlite 真实落库，种子模式沿用 tests/agents/test_agent_executor.py：
- 终态 run + 非终态关联消息 → 重放 writeback 修复占位消息；
- RUNNING 超阈值且所有任务 lease 过期 → run 判 FAILED 并重放；
- 有活 lease 的 RUNNING run 与已 CANCELLED 的消息绝不动。
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from proseforge.application.messages.sweeper import sweep_stale_run_messages
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentArtifactModel,
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationModel,
    MessageModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def sweeper_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'sweeper.db').as_posix()}"
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


async def _seed(
    settings: Settings,
    *,
    run_status: str,
    message_status: str = "PENDING",
    terminal_reason: str | None = None,
    updated_at: datetime | None = None,
    tasks: list[dict[str, object]] | None = None,
    artifacts: list[dict[str, object]] | None = None,
    events: list[tuple[str, dict[str, object]]] | None = None,
    goal: str | None = None,
) -> dict[str, str]:
    """一个 run + 一条 agent_run_id 关联的占位 assistant 消息 + 指定任务/产物/事件。"""
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            user = await uow.users.create(f"sweeper-{uuid.uuid4().hex[:8]}@example.local", "hash-not-used", "ADMIN")
            now = datetime.now(UTC)
            uow.session.add(ProjectModel(id="project-1", owner_id=user.id, slug="project-1", title="Sweeper Test Project"))
            await uow.session.flush()
            run = AgentRunModel(
                id=new_id(), user_id=user.id, project_id="project-1", goal=goal, goal_hash="g" * 64,
                graph_revision=1, status=run_status, budget_limit=1000, terminal_reason=terminal_reason,
                created_at=now, updated_at=updated_at or now,
            )
            uow.session.add(run)
            await uow.session.flush()
            task_id_by_key: dict[str, str] = {}
            for spec in tasks or []:
                task = AgentTaskModel(
                    id=new_id(), run_id=run.id, task_key=str(spec["task_key"]), role="chief_planner",
                    status=str(spec.get("status", "PENDING")), token_budget=1, depends_on="[]",
                    lease_owner=spec.get("lease_owner"), lease_expires_at=spec.get("lease_expires_at"),
                )
                uow.session.add(task)
                await uow.session.flush()
                task_id_by_key[str(spec["task_key"])] = task.id
            for artifact in artifacts or []:
                uow.session.add(AgentArtifactModel(
                    id=new_id(), run_id=run.id, task_id=task_id_by_key.get(str(artifact.get("task_key", ""))),
                    artifact_type=str(artifact.get("artifact_type", "scene_draft")), sha256="s" * 64,
                    preview="", payload=json.dumps(artifact["payload"], ensure_ascii=False),
                ))
            for sequence, (event_type, event_payload) in enumerate(events or [], start=1):
                uow.session.add(AgentEventModel(
                    id=new_id(), run_id=run.id, sequence=sequence, event_type=event_type,
                    payload=json.dumps(event_payload, sort_keys=True),
                ))
            run.event_cursor = len(events or [])
            uow.session.add(ConversationModel(id="conv-1", project_id="project-1", title="swarm"))
            await uow.session.flush()
            uow.session.add(ConversationBranchModel(id="branch-1", conversation_id="conv-1", name="Main"))
            await uow.session.flush()
            uow.session.add(MessageModel(id="msg-1", branch_id="branch-1", role="assistant", content="", sequence_no=1, status=message_status, agent_run_id=run.id))
            await uow.commit()
            return {"run_id": run.id, "message_id": "msg-1", "user_id": user.id}
    finally:
        await engine.dispose()


async def _sweep(settings: Settings, **kwargs) -> int:
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        return await sweep_stale_run_messages(session_factory, settings, **kwargs)
    finally:
        await engine.dispose()


async def _read_message(settings: Settings, message_id: str) -> dict[str, str]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            message = await uow.session.get(MessageModel, message_id)
            return {"status": message.status, "content": message.content}
    finally:
        await engine.dispose()


async def _read_run(settings: Settings, run_id: str) -> dict[str, object]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            events = [
                event.event_type
                for event in await uow.session.scalars(select(AgentEventModel).where(AgentEventModel.run_id == run_id))
            ]
            return {"status": run.status, "terminal_reason": run.terminal_reason, "events": events}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_run_with_stranded_message_replays_writeback(sweeper_settings):
    """run 已 COMPLETED 但 writeback commit 失败 → 消息悬挂 PENDING；sweeper
    重放 writeback 把消息补成 COMPLETED。"""
    seeded = await _seed(sweeper_settings, run_status="COMPLETED", tasks=[{"task_key": "planner", "status": "SUCCEEDED"}])

    repaired = await _sweep(sweeper_settings)

    assert repaired == 1
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "COMPLETED"
    assert "总调度" in message["content"]


@pytest.mark.asyncio
async def test_failed_terminal_run_message_repaired_as_failed(sweeper_settings):
    seeded = await _seed(sweeper_settings, run_status="FAILED", terminal_reason="task(s) failed without retry", tasks=[{"task_key": "planner", "status": "FAILED"}])

    repaired = await _sweep(sweeper_settings)

    assert repaired == 1
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "FAILED"
    assert "批次未完成" in message["content"]


@pytest.mark.asyncio
async def test_stale_running_run_marked_failed_and_message_repaired(sweeper_settings):
    """worker 死亡：run 卡 RUNNING（updated_at 超阈值、任务 lease 已过期）→
    sweeper 判 FAILED 并把悬挂消息补成 FAILED。"""
    stale = datetime.now(UTC) - timedelta(hours=1)
    seeded = await _seed(
        sweeper_settings,
        run_status="RUNNING",
        updated_at=stale,
        tasks=[{"task_key": "planner", "status": "RUNNING", "lease_owner": "celery:dead", "lease_expires_at": stale}],
    )

    repaired = await _sweep(sweeper_settings)

    assert repaired == 1
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert run["status"] == "FAILED"
    assert run["terminal_reason"] == "executor lost; marked failed by message sweeper"
    assert "run.failed" in run["events"]
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "FAILED"


@pytest.mark.asyncio
async def test_running_run_with_live_lease_is_untouched(sweeper_settings):
    """还有活 lease 的 RUNNING run 是在飞执行：sweeper 绝不动它和它的消息。"""
    stale = datetime.now(UTC) - timedelta(hours=1)
    live = datetime.now(UTC) + timedelta(minutes=5)
    seeded = await _seed(
        sweeper_settings,
        run_status="RUNNING",
        updated_at=stale,
        tasks=[{"task_key": "planner", "status": "RUNNING", "lease_owner": "celery:alive", "lease_expires_at": live}],
    )

    repaired = await _sweep(sweeper_settings)

    assert repaired == 0
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert run["status"] == "RUNNING"
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "PENDING"


@pytest.mark.asyncio
async def test_cancelled_message_is_never_replayed(sweeper_settings):
    """用户已取消的消息是终态：即使 run 后来 FAILED，sweeper 也不重放。"""
    seeded = await _seed(sweeper_settings, run_status="FAILED", message_status="CANCELLED", tasks=[{"task_key": "planner", "status": "FAILED"}])

    repaired = await _sweep(sweeper_settings)

    assert repaired == 0
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "CANCELLED"
    assert message["content"] == ""


@pytest.mark.asyncio
async def test_healthy_terminal_run_needs_no_repair(sweeper_settings):
    """消息已是终态的 run 不在修复范围（幂等：正常路径零动作）。"""
    seeded = await _seed(sweeper_settings, run_status="COMPLETED", message_status="COMPLETED", tasks=[{"task_key": "planner", "status": "SUCCEEDED"}])

    repaired = await _sweep(sweeper_settings)

    assert repaired == 0
    message = await _read_message(sweeper_settings, seeded["message_id"])
    assert message["status"] == "COMPLETED"
    assert message["content"] == ""


# ---------------------------------------------------------------------------
# 章节写回重放：run COMPLETED 但 writeback commit 失败（正文永久丢失）
# ---------------------------------------------------------------------------

SCENE_ARTIFACT = {"title": "雨夜", "content": "雨夜，主角提着青铜钥匙回城。" * 10}


async def _read_chapters(settings: Settings) -> list[dict[str, object]]:
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            return [
                {"chapter_no": chapter.chapter_no, "title": chapter.title, "content": content}
                for chapter, content in await uow.session.execute(
                    select(ChapterModel, ChapterVersionModel.content)
                    .join(ChapterVersionModel, ChapterVersionModel.chapter_id == ChapterModel.id)
                )
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_run_with_failed_chapter_writeback_is_replayed(sweeper_settings):
    """写管线 run 已 COMPLETED（chapter.writeback_failed 事件在）但无
    chapter.written_back → sweeper 重放共享写回，章节正文落库。"""
    seeded = await _seed(
        sweeper_settings,
        run_status="COMPLETED",
        message_status="COMPLETED",  # 消息已修复：只触发章节写回这一趟
        tasks=[{"task_key": "scene", "status": "SUCCEEDED"}],
        artifacts=[{"task_key": "scene", "payload": SCENE_ARTIFACT}],
        events=[("chapter.writeback_failed", {"error": "OperationalError"})],
        goal="写第一章",
    )

    repaired = await _sweep(sweeper_settings)

    assert repaired == 1
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert "chapter.written_back" in run["events"]
    chapters = await _read_chapters(sweeper_settings)
    assert len(chapters) == 1
    assert chapters[0]["chapter_no"] == 1
    assert chapters[0]["title"] == "雨夜"
    assert chapters[0]["content"] == SCENE_ARTIFACT["content"]


@pytest.mark.asyncio
async def test_chapter_writeback_replay_is_idempotent(sweeper_settings):
    """重放成功后再扫：chapter.written_back 事件在 → 不再是候选，绝不二次写版本。"""
    seeded = await _seed(
        sweeper_settings,
        run_status="COMPLETED",
        message_status="COMPLETED",
        tasks=[{"task_key": "scene", "status": "SUCCEEDED"}],
        artifacts=[{"task_key": "scene", "payload": SCENE_ARTIFACT}],
        goal="写第一章",
    )

    assert await _sweep(sweeper_settings) == 1
    assert await _sweep(sweeper_settings) == 0  # 第二轮零动作

    chapters = await _read_chapters(sweeper_settings)
    assert len(chapters) == 1  # 仍然只有一个章节一个版本
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert run["events"].count("chapter.written_back") == 1


@pytest.mark.asyncio
async def test_chapter_writeback_replay_cooldown_after_failure(sweeper_settings):
    """重放失败（无任何产物可写）→ 落 chapter.writeback_replayed 冷却标记，
    冷却期内不再重放；冷却过后允许再试。"""
    seeded = await _seed(
        sweeper_settings,
        run_status="COMPLETED",
        message_status="COMPLETED",
        tasks=[{"task_key": "scene", "status": "SUCCEEDED"}],  # 无 artifacts：写回必然无果
    )

    assert await _sweep(sweeper_settings) == 1
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert "chapter.writeback_replayed" in run["events"]

    # 冷却期内：同一 run 不重复重放
    assert await _sweep(sweeper_settings) == 0
    # 冷却参数放宽后立即允许重试（证明冷却而非永久放弃）
    engine, session_factory = create_engine_and_sessionmaker(sweeper_settings)
    try:
        from proseforge.application.messages.sweeper import (
            sweep_missed_chapter_writebacks,
        )

        assert await sweep_missed_chapter_writebacks(session_factory, sweeper_settings, cooldown_seconds=0) == 1
    finally:
        await engine.dispose()
    run = await _read_run(sweeper_settings, seeded["run_id"])
    assert run["events"].count("chapter.writeback_replayed") == 2


@pytest.mark.asyncio
async def test_written_back_run_is_not_a_replay_candidate(sweeper_settings):
    """正常写回的 COMPLETED 写管线 run（written_back 事件在）不进重放。"""
    await _seed(
        sweeper_settings,
        run_status="COMPLETED",
        message_status="COMPLETED",
        tasks=[{"task_key": "scene", "status": "SUCCEEDED"}],
        artifacts=[{"task_key": "scene", "payload": SCENE_ARTIFACT}],
        events=[("chapter.written_back", {"chapter_id": "c-1", "chapter_no": 1})],
    )

    assert await _sweep(sweeper_settings) == 0
    assert await _read_chapters(sweeper_settings) == []
