"""记忆服务（application/agents/memory_service.py）宿主可跑测试。

sqlite+aiosqlite 真实落库（模式沿用 tests/agents/test_agent_executor.py），
覆盖：PENDING 候选写入与 revision 计数、用户审批翻转、状态过滤、
load_memory_slice 的作用域/限界，以及 activate_memory_fact 对全角色恒拒。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.agents.memory_service import (
    MEMORY_SLICE_LIMIT,
    PROJECT_WIDE_RUN,
    assert_agent_activation_denied,
    decide_memory,
    decode_value,
    list_memories,
    load_memory_slice,
    memory_view,
    propose_memory,
)
from proseforge.domain.agents.policy import PolicyDenied
from proseforge.domain.agents.roles import AgentRole
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.agents import (
    AgentMemoryModel,
    AgentRunModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings, get_settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def memory_settings(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'memory.db').as_posix()}"
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


async def _propose(settings: Settings, **overrides) -> dict[str, object]:
    project_id = overrides.get("project_id", "project-1")
    run_id = overrides.get("run_id", "run-1")
    engine, factory = create_engine_and_sessionmaker(settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            # FK parents: agent_memories -> projects / agent_runs. Seed idempotently
            # (_propose reuses the same file DB across calls) and flush each parent
            # before its children; PROJECT_WIDE_RUN is the "" sentinel run row.
            if await uow.session.get(ProjectModel, project_id) is None:
                uow.session.add(ProjectModel(id=project_id, owner_id="u1", slug=project_id, title="Memory Test Project"))
                await uow.session.flush()
            if await uow.session.get(AgentRunModel, run_id) is None:
                now = datetime.now(UTC)
                uow.session.add(AgentRunModel(
                    id=run_id, user_id="u1", project_id=project_id, goal_hash="g" * 64,
                    graph_revision=1, status="RUNNING", budget_limit=1000,
                    created_at=now, updated_at=now,
                ))
                await uow.session.flush()
            row = await propose_memory(
                uow.session,
                project_id=project_id,
                run_id=run_id,
                memory_key=overrides.get("memory_key", "林雪:惯用手"),
                value=overrides.get("value", "左撇子"),
                source_artifact_id=overrides.get("source_artifact_id", "artifact-1"),
                confidence=overrides.get("confidence", 0.7),
            )
            await uow.commit()
            return {"id": row.id, "revision": decode_value(row)["revision"]}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_propose_writes_pending_candidate_with_confidence_and_revision(memory_settings):
    first = await _propose(memory_settings)
    second = await _propose(memory_settings)  # 同 project+key：revision 递增

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            row = await uow.session.get(AgentMemoryModel, first["id"])
            assert row.status == "PENDING"
            envelope = decode_value(row)
            assert envelope == {"value": "左撇子", "confidence": 0.7, "revision": 1}
            assert second["revision"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_decide_flips_status_and_list_filters(memory_settings):
    accepted = await _propose(memory_settings, memory_key="顾岩:性格", value="沉默寡言")
    rejected = await _propose(memory_settings, memory_key="林雪:年龄", value="二十二岁")
    await _propose(memory_settings, memory_key="北渡:地点", value="江北渡口")

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            decide_memory(await uow.session.get(AgentMemoryModel, accepted["id"]), "accept")
            decide_memory(await uow.session.get(AgentMemoryModel, rejected["id"]), "reject")
            await uow.commit()
        async with SqlAlchemyUnitOfWork(factory) as uow:
            accepted_rows = await list_memories(uow.session, project_id="project-1", status="ACCEPTED")
            assert [row.memory_key for row in accepted_rows] == ["顾岩:性格"]
            rejected_rows = await list_memories(uow.session, project_id="project-1", status="REJECTED")
            assert [row.memory_key for row in rejected_rows] == ["林雪:年龄"]
            pending_rows = await list_memories(uow.session, project_id="project-1", run_id="run-1", status="PENDING")
            assert [row.memory_key for row in pending_rows] == ["北渡:地点"]
            view = memory_view(pending_rows[0])
            assert view["status"] == "PENDING" and view["value"] == "江北渡口" and view["source_artifact_id"] == "artifact-1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_memory_slice_scopes_accepted_run_and_project_wide(memory_settings):
    await _propose(memory_settings, memory_key="run-fact", value="本 run 事实")
    await _propose(memory_settings, memory_key="project-fact", value="项目级事实", run_id=PROJECT_WIDE_RUN)
    await _propose(memory_settings, memory_key="other-run-fact", value="其他 run", run_id="run-2")
    await _propose(memory_settings, memory_key="pending-fact", value="未批准")

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            for row in await uow.session.scalars(select(AgentMemoryModel)):
                if row.memory_key != "pending-fact":
                    decide_memory(row, "accept")
            await uow.commit()
        slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(factory), {"id": "run-1", "project_id": "project-1"})
        assert {item["fact_key"] for item in slice_} == {"run-fact", "project-fact"}
        assert all(len(str(item["value"])) <= 200 for item in slice_)
    finally:
        await engine.dispose()

    # 无 uow_factory / 无 project_id：空切片（纯内存 context 不炸）
    assert await load_memory_slice(None, {"id": "run-1", "project_id": "project-1"}) == []
    assert await load_memory_slice(lambda: None, {"id": "run-1"}) == []


@pytest.mark.asyncio
async def test_load_memory_slice_is_bounded(memory_settings):
    for index in range(MEMORY_SLICE_LIMIT + 4):
        await _propose(memory_settings, memory_key=f"fact-{index:02d}", value="x" * 500)

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            for row in await uow.session.scalars(select(AgentMemoryModel)):
                decide_memory(row, "accept")
            await uow.commit()
        slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(factory), {"id": "run-1", "project_id": "project-1"})
        assert len(slice_) == MEMORY_SLICE_LIMIT
        assert all(len(str(item["value"])) == 200 for item in slice_)
    finally:
        await engine.dispose()


def test_activate_memory_fact_is_denied_for_every_role():
    for role in AgentRole:
        with pytest.raises(PolicyDenied):
            assert_agent_activation_denied(role)


@pytest.mark.asyncio
async def test_propose_pipeline_status_accepted_activates_directly(memory_settings):
    """管线沉淀通道：status="ACCEPTED" 落库即激活，不走人工审批；非法状态拒写。"""
    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            uow.session.add(ProjectModel(id="project-1", owner_id="u1", slug="project-1", title="Memory Test Project"))
            await uow.session.flush()
            row = await propose_memory(
                uow.session, project_id="project-1", run_id=PROJECT_WIDE_RUN,
                memory_key="人物状态·林雪", value="第3章：情绪：决绝；精神：疲惫",
                source_artifact_id="chapter-3", confidence=0.6, status="ACCEPTED",
            )
            await uow.commit()
            assert row.status == "ACCEPTED"
        with pytest.raises(ValueError, match="unknown memory status"):
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await propose_memory(
                    uow.session, project_id="project-1", run_id=PROJECT_WIDE_RUN,
                    memory_key="k", value="v", source_artifact_id="a", status="BOGUS",
                )
        # 立即可被切片查到（项目级作用域对任意 run 可见）
        slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(factory), {"id": "run-9", "project_id": "project-1"})
        assert [item["fact_key"] for item in slice_] == ["人物状态·林雪"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_memory_slice_dedupes_to_latest_revision(memory_settings):
    """同 key 多 revision（管线每章重写状态事实）：切片只留最新 revision 的值。"""
    await _propose(memory_settings, memory_key="人物状态·林雪", value="第1章：平静")
    await _propose(memory_settings, memory_key="人物状态·林雪", value="第2章：动摇")
    await _propose(memory_settings, memory_key="人物状态·林雪", value="第3章：决绝")

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            for row in await uow.session.scalars(select(AgentMemoryModel)):
                decide_memory(row, "accept")
            await uow.commit()
        slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(factory), {"id": "run-1", "project_id": "project-1"})
        assert len(slice_) == 1
        assert slice_[0]["value"] == "第3章：决绝"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_memory_slice_prefers_most_recent(memory_settings):
    """超限取最近写入的 N 条——「先查再想」查的是当下最相关，不是最早入库。"""
    for index in range(MEMORY_SLICE_LIMIT + 4):
        await _propose(memory_settings, memory_key=f"fact-{index:02d}", value=f"值-{index:02d}")

    engine, factory = create_engine_and_sessionmaker(memory_settings)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            for row in await uow.session.scalars(select(AgentMemoryModel)):
                decide_memory(row, "accept")
            await uow.commit()
        slice_ = await load_memory_slice(lambda: SqlAlchemyUnitOfWork(factory), {"id": "run-1", "project_id": "project-1"})
        keys = [item["fact_key"] for item in slice_]
        assert keys == [f"fact-{index:02d}" for index in range(4, MEMORY_SLICE_LIMIT + 4)]
    finally:
        await engine.dispose()
