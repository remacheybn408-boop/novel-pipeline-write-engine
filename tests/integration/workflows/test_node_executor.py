"""Executor-level contracts for V2 workflow runs (V2-008).

Runs against PostgreSQL in the batch (PROSEFORGE_TEST_DATABASE_URL) and
against file-backed SQLite locally (native profile), mirroring
tests/integration/workflows/test_generate_novel_context_budget.py.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.application.workflows.definition_service import (
    WorkflowDefinitionService,
)
from proseforge.application.workflows.executor import (
    DEFAULT_HANDLERS,
    NodeOutcome,
    WorkflowRunExecutor,
)
from proseforge.application.workflows.recover_run import (
    queued_definition_run_ids,
    recover_expired_workflow_nodes,
)
from proseforge.application.workflows.run_service import WorkflowRunService
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.export import ExportManifestModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import WorkflowRunModel
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel
from proseforge.infrastructure.database.repositories.workflow import (
    SqlAlchemyWorkflowRepository,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

OWNER = "executor-user"


@pytest_asyncio.fixture
async def session_factory(tmp_path) -> async_sessionmaker[AsyncSession]:
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    profile = "test" if database_url else "native"
    if not database_url:
        database_url = f"sqlite+aiosqlite:///{(tmp_path / 'executor.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        runtime_profile=profile,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    engine, factory = create_engine_and_sessionmaker(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _executor(session_factory, handlers=None) -> WorkflowRunExecutor:
    return WorkflowRunExecutor(lambda: SqlAlchemyUnitOfWork(session_factory), handlers=handlers)


async def _seed_project(session_factory) -> tuple[str, str]:
    suffix = uuid4().hex
    project_id = f"exec-project-{suffix}"
    chapter_id = f"exec-chapter-{suffix}"
    version_id = f"exec-version-{suffix}"
    async with session_factory() as session:
        # Flush parents before children: chapters/chapter_versions now FK-reference
        # projects.id/chapters.id and the unit of work does not order these
        # relationship-free mappers by table FKs.
        session.add(ProjectModel(id=project_id, owner_id=OWNER, slug=f"exec-{suffix}", title="Executor"))
        await session.flush()
        session.add(ChapterModel(id=chapter_id, project_id=project_id, chapter_no=1, title="Chapter 1", status="DRAFT", active_version_id=version_id))
        await session.flush()
        session.add(ChapterVersionModel(id=version_id, chapter_id=chapter_id, version_no=1, content="Canonical manuscript", content_hash=hashlib.sha256(b"Canonical manuscript").hexdigest(), word_count=2))
        await session.commit()
    return project_id, chapter_id


async def _create_run(session_factory, project_id: str, definition: dict[str, object], **limits) -> str:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        row = await WorkflowDefinitionService(uow).create(project_id, OWNER, f"exec-flow-{uuid4().hex[:8]}", definition)
        run, _nodes = await WorkflowRunService(uow).create(row.id, OWNER, **limits)
        await uow.commit()
        return str(run.id)


async def _read_run(session_factory, run_id: str) -> dict[str, object]:
    async with session_factory() as session:
        run = await session.get(WorkflowRunModel, run_id)
        assert run is not None
        return {"status": run.status, "used_tokens": run.used_tokens, "estimated_cost": float(run.estimated_cost or 0), "lease_owner": run.lease_owner, "last_error": run.last_error}


async def _read_nodes(session_factory, run_id: str) -> dict[str, dict[str, object]]:
    async with session_factory() as session:
        rows = list(await session.scalars(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id)))
        return {
            row.node_key: {
                "status": row.status,
                "used_tokens": row.used_tokens,
                "reserved_tokens": row.reserved_tokens,
                "used_cost": row.used_cost,
                "reserved_cost": row.reserved_cost,
                "retry_count": row.retry_count,
                "checkpoint": row.checkpoint_json,
                "lease_owner": row.lease_owner,
            }
            for row in rows
        }


async def _read_events(session_factory, run_id: str) -> list[dict[str, object]]:
    async with session_factory() as session:
        return await SqlAlchemyWorkflowRepository(session).events(run_id)


async def _active_content(session_factory, chapter_id: str) -> tuple[int, str]:
    async with session_factory() as session:
        chapter = await session.get(ChapterModel, chapter_id)
        assert chapter is not None and chapter.active_version_id is not None
        version = await session.get(ChapterVersionModel, chapter.active_version_id)
        assert version is not None
        return version.version_no, version.content


@pytest.mark.asyncio
async def test_executor_walks_all_six_node_kinds_in_topological_order(session_factory):
    project_id, chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [
            {"id": "intake", "kind": "intake", "config": {"outline": {"title": "T", "genre": "fantasy", "characters": ["a"], "point_of_view": "third"}}},
            {"id": "plan", "kind": "plan", "config": {"volumes": 1, "chapters_per_volume": 1, "word_target": 1000}},
            {"id": "write", "kind": "write", "config": {"chapter_id": chapter_id, "content": "Draft manuscript"}},
            {"id": "review", "kind": "review", "config": {"chapter_id": chapter_id}},
            {"id": "rewrite", "kind": "rewrite", "config": {"chapter_id": chapter_id, "content": "Revised manuscript"}},
            {"id": "export", "kind": "export", "config": {"format": "txt", "template": "archive", "title": "Executor"}},
        ],
        "edges": [
            {"source": "intake", "target": "plan"},
            {"source": "plan", "target": "write"},
            {"source": "write", "target": "review"},
            {"source": "review", "target": "rewrite"},
            {"source": "rewrite", "target": "export"},
        ],
    }
    run_id = await _create_run(session_factory, project_id, definition)

    result = await _executor(session_factory).execute(run_id, f"celery:v2:{run_id}")

    assert result == "completed"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "COMPLETED"
    assert run["lease_owner"] is None
    nodes = await _read_nodes(session_factory, run_id)
    assert set(nodes) == {"intake", "plan", "write", "review", "rewrite", "export"}
    assert all(node["status"] == "COMPLETED" for node in nodes.values())
    assert all(node["reserved_tokens"] == 0 and node["reserved_cost"] == 0 for node in nodes.values())

    events = await _read_events(session_factory, run_id)
    started = [event["data"]["node_key"] for event in events if event["event"] == "node.started"]
    assert started == ["intake", "plan", "write", "review", "rewrite", "export"]
    assert events[-1]["event"] == "run.completed"  # 最后事件必为 terminal 且已持久化

    version_no, content = await _active_content(session_factory, chapter_id)
    assert (version_no, content) == (3, "Revised manuscript")  # write=v2，rewrite=v3 且保持 active

    import json

    export_checkpoint = json.loads(str(nodes["export"]["checkpoint"]))
    async with session_factory() as session:
        manifest = await session.get(ExportManifestModel, export_checkpoint["manifest_id"])
        assert manifest is not None
        assert manifest.file_sha256 == export_checkpoint["file_sha256"]
        assert manifest.byte_size == export_checkpoint["byte_size"] > 0

    again = await _executor(session_factory).execute(run_id, f"celery:v2:{run_id}")
    assert again == "completed"  # 幂等重入：terminal run 不再执行任何节点


@pytest.mark.asyncio
async def test_executor_records_actual_usage_releases_reservation_and_blocks_on_overrun(session_factory):
    project_id, _chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [
            {"id": "a", "kind": "write", "config": {"content": "A", "reserved_tokens": 60}},
            {"id": "b", "kind": "write", "config": {"content": "B", "reserved_tokens": 30}},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    run_id = await _create_run(session_factory, project_id, definition, token_limit=100)

    async def stub_write(uow, run, node, config):
        del uow, run, node
        return NodeOutcome(used_tokens=90 if config["content"] == "A" else 10, used_cost=0.7, checkpoint={"content": config["content"]})

    result = await _executor(session_factory, handlers={**DEFAULT_HANDLERS, "write": stub_write}).execute(run_id, f"celery:v2:{run_id}")

    assert result == "budget-blocked"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "BUDGET_BLOCKED"
    assert run["used_tokens"] == 90  # 只记节点 a 的实际用量
    assert run["estimated_cost"] == pytest.approx(0.7)
    assert run["lease_owner"] is None
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["a"]["status"] == "COMPLETED"
    assert nodes["a"]["used_tokens"] == 90
    assert nodes["a"]["reserved_tokens"] == 0  # 未用预留已释放
    assert nodes["b"]["status"] == "BLOCKED"  # 90 + 30 > 100 → 超额，handler 未执行
    events = await _read_events(session_factory, run_id)
    assert events[-1]["event"] == "run.budget_blocked"
    assert events[-1]["data"]["node_key"] == "b"

    async with session_factory() as session:  # 控制面 retry：BLOCKED 节点回 PENDING 后仍超额 → 重新阻塞，不死循环
        node_row = await session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.node_key == "b"))
        assert node_row is not None
        node_row.status = "PENDING"
        node_row.reserved_tokens = 30
        node_row.retry_count += 1
        node_row.updated_at = datetime.now(UTC)
        run_row = await session.get(WorkflowRunModel, run_id)
        assert run_row is not None
        run_row.status = "RUNNING"
        await session.commit()

    retried = await _executor(session_factory, handlers={**DEFAULT_HANDLERS, "write": stub_write}).execute(run_id, f"celery:v2:{run_id}")
    assert retried == "budget-blocked"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "BUDGET_BLOCKED"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["b"]["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_pause_between_nodes_stops_cleanly_and_resume_continues(session_factory):
    project_id, _chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [
            {"id": "a", "kind": "write", "config": {"content": "A"}},
            {"id": "b", "kind": "write", "config": {"content": "B"}},
            {"id": "c", "kind": "write", "config": {"content": "C"}},
        ],
        "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}],
    }
    run_id = await _create_run(session_factory, project_id, definition)

    async def pausing_write(uow, run, node, config):
        del uow, node
        if config["content"] == "A":
            run.status = "PAUSED"  # 模拟节点执行期间到达的 pause，节点边界生效
        return NodeOutcome(checkpoint={"content": config["content"]})

    result = await _executor(session_factory, handlers={**DEFAULT_HANDLERS, "write": pausing_write}).execute(run_id, f"celery:v2:{run_id}")

    assert result == "paused"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "PAUSED"
    assert run["lease_owner"] is None
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["a"]["status"] == "COMPLETED"
    assert nodes["b"]["status"] == nodes["c"]["status"] == "PENDING"

    async with session_factory() as session:  # 控制面 resume：RUNNING 后重新入队执行器
        run_row = await session.get(WorkflowRunModel, run_id)
        assert run_row is not None
        run_row.status = "RUNNING"
        await session.commit()

    resumed = await _executor(session_factory, handlers={**DEFAULT_HANDLERS, "write": pausing_write}).execute(run_id, f"celery:v2:{run_id}")
    assert resumed == "completed"
    nodes = await _read_nodes(session_factory, run_id)
    assert all(node["status"] == "COMPLETED" for node in nodes.values())


@pytest.mark.asyncio
async def test_failed_node_marks_run_and_retry_continues_from_that_node(session_factory):
    project_id, _chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [
            {"id": "a", "kind": "write", "config": {"content": "A"}},
            {"id": "b", "kind": "write", "config": {"content": "B"}},
        ],
        "edges": [{"source": "a", "target": "b"}],
    }
    run_id = await _create_run(session_factory, project_id, definition)
    attempts = {"b": 0}

    async def flaky_write(uow, run, node, config):
        del uow, run, node
        if config["content"] == "B" and attempts["b"] == 0:
            attempts["b"] += 1
            raise RuntimeError("boom")
        return NodeOutcome(checkpoint={"content": config["content"]})

    handlers = {**DEFAULT_HANDLERS, "write": flaky_write}
    result = await _executor(session_factory, handlers=handlers).execute(run_id, f"celery:v2:{run_id}")

    assert result == "failed"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "FAILED"
    assert run["last_error"] == "RuntimeError"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["a"]["status"] == "COMPLETED"
    assert nodes["b"]["status"] == "FAILED"
    events = await _read_events(session_factory, run_id)
    assert [event["event"] for event in events[-2:]] == ["node.failed", "run.failed"]

    async with session_factory() as session:  # 控制面 retry：FAILED 节点回 PENDING、retry_count 递增
        node_row = await session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.node_key == "b"))
        assert node_row is not None
        node_row.status = "PENDING"
        node_row.retry_count += 1
        node_row.updated_at = datetime.now(UTC)
        run_row = await session.get(WorkflowRunModel, run_id)
        assert run_row is not None
        run_row.status = "RUNNING"
        await session.commit()

    retried = await _executor(session_factory, handlers=handlers).execute(run_id, f"celery:v2:{run_id}")
    assert retried == "completed"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["b"]["status"] == "COMPLETED"
    assert nodes["b"]["retry_count"] == 1
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_recovery_requeues_expired_node_and_executor_picks_it_up(session_factory):
    project_id, chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [
            {"id": "intake", "kind": "intake", "config": {"outline": {"title": "T"}}},
            {"id": "write", "kind": "write", "config": {"chapter_id": chapter_id, "content": "Recovered draft"}},
        ],
        "edges": [{"source": "intake", "target": "write"}],
    }
    run_id = await _create_run(session_factory, project_id, definition)
    expired = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as session:  # 模拟 worker 崩溃：节点与 run 租约都过期
        run_row = await session.get(WorkflowRunModel, run_id)
        assert run_row is not None
        run_row.lease_owner = "dead-worker"
        run_row.lease_expires_at = expired
        node_row = await session.scalar(select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id == run_id, WorkflowNodeStateModel.node_key == "write"))
        assert node_row is not None
        node_row.status = "RUNNING"
        node_row.lease_owner = "dead-worker"
        node_row.lease_expires_at = expired
        node_row.retry_count = 1
        node_row.updated_at = expired
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await recover_expired_workflow_nodes(uow) == 1
        assert await queued_definition_run_ids(uow) == [run_id]
        await uow.commit()

    run = await _read_run(session_factory, run_id)
    assert run["status"] == "QUEUED"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["write"]["status"] == "PENDING"
    assert nodes["write"]["retry_count"] == 2

    result = await _executor(session_factory).execute(run_id, f"celery:v2:{run_id}")

    assert result == "completed"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "COMPLETED"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["write"]["status"] == "COMPLETED"
    assert nodes["write"]["retry_count"] == 2  # 执行器不动 retry_count
    events = await _read_events(session_factory, run_id)
    names = [event["event"] for event in events]
    assert names.index("run.recovered") < names.index("run.completed")
    version_no, content = await _active_content(session_factory, chapter_id)
    assert (version_no, content) == (2, "Recovered draft")


@pytest.mark.asyncio
async def test_statically_budget_blocked_run_is_not_executed(session_factory):
    project_id, chapter_id = await _seed_project(session_factory)
    definition = {
        "nodes": [{"id": "write", "kind": "write", "config": {"chapter_id": chapter_id, "content": "Should never land", "reserved_tokens": 500}}],
        "edges": [],
    }
    run_id = await _create_run(session_factory, project_id, definition, token_limit=10)

    result = await _executor(session_factory).execute(run_id, f"celery:v2:{run_id}")

    assert result == "budget-blocked"
    run = await _read_run(session_factory, run_id)
    assert run["status"] == "BUDGET_BLOCKED"
    nodes = await _read_nodes(session_factory, run_id)
    assert nodes["write"]["status"] == "BLOCKED"
    version_no, content = await _active_content(session_factory, chapter_id)
    assert (version_no, content) == (1, "Canonical manuscript")  # 无半成品 version
