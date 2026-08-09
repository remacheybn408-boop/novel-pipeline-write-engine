"""Persistence-level recovery contracts for V2 workflow runs."""

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
from proseforge.application.workflows.recover_run import recover_expired_workflow_nodes
from proseforge.application.workflows.run_service import WorkflowRunService
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel
from proseforge.infrastructure.database.repositories.workflow import (
    SqlAlchemyWorkflowRepository,
)
from proseforge.infrastructure.database.session import create_engine_and_sessionmaker
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings


@pytest_asyncio.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PROSEFORGE_TEST_DATABASE_URL is required (integration tests run in the batch)")
    settings = Settings(
        database_url=database_url,
        redis_url=os.environ.get("PROSEFORGE_TEST_REDIS_URL", "redis://redis:6379/0"),
    )
    engine, factory = create_engine_and_sessionmaker(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_replays_only_events_after_snapshot_cursor(session_factory):
    suffix = uuid4().hex
    project_id = f"refresh-project-{suffix}"
    async with session_factory() as session:
        session.add(
            ProjectModel(
                id=project_id,
                owner_id="refresh-user",
                slug=f"refresh-{suffix}",
                title="Refresh",
            )
        )
        repository = SqlAlchemyWorkflowRepository(session)
        run = await repository.create(project_id, "DEFINITION:1")
        await repository.transition(run, "RUNNING")
        await repository.append_event(run.id, "node.started", {"node_key": "draft"})
        snapshot_cursor = (await repository.events(run.id))[-1]["id"]
        await repository.append_event(run.id, "node.completed", {"node_key": "draft"})
        await repository.transition(run, "COMPLETED")
        await session.commit()

    async with session_factory() as session:
        replay = await SqlAlchemyWorkflowRepository(session).events(run.id, int(snapshot_cursor))

    assert [event["event"] for event in replay] == ["node.completed", "COMPLETED"]
    assert [event["id"] for event in replay] == sorted({event["id"] for event in replay})
    assert replay[-1]["event"] == "COMPLETED"


@pytest.mark.asyncio
async def test_expired_lease_recovery_requeues_checkpointed_node_and_increments_retry(session_factory):
    suffix = uuid4().hex
    project_id = f"lease-project-{suffix}"
    node_id = f"lease-node-{suffix}"
    expired = datetime.now(UTC) - timedelta(seconds=1)
    now = datetime.now(UTC)
    async with session_factory() as session:
        session.add(
            ProjectModel(
                id=project_id,
                owner_id="lease-user",
                slug=f"lease-{suffix}",
                title="Lease",
            )
        )
        repository = SqlAlchemyWorkflowRepository(session)
        run = await repository.create(project_id, "DEFINITION:1")
        await repository.transition(run, "RUNNING")
        run.lease_owner = "dead-worker"
        run.lease_expires_at = expired
        session.add(
            WorkflowNodeStateModel(
                id=node_id,
                run_id=run.id,
                node_key="draft",
                status="RUNNING",
                checkpoint_json='{"cursor":2}',
                lease_owner="dead-worker",
                lease_expires_at=expired,
                retry_count=1,
                reserved_tokens=200,
                used_tokens=80,
                reserved_cost=2,
                used_cost=0.8,
                updated_at=now,
            )
        )
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await recover_expired_workflow_nodes(uow) == 1
        await uow.commit()

    async with session_factory() as session:
        recovered_run = await SqlAlchemyWorkflowRepository(session).get_owned(run.id, "lease-user")
        recovered_node = await session.scalar(
            select(WorkflowNodeStateModel).where(WorkflowNodeStateModel.id == node_id)
        )

    assert recovered_run is not None and recovered_run.status == "QUEUED"
    assert recovered_run.lease_owner is None and recovered_run.lease_expires_at is None
    assert recovered_node is not None
    assert recovered_node.status == "PENDING"
    assert recovered_node.retry_count == 2
    assert recovered_node.checkpoint_json == '{"cursor":2}'
    assert recovered_node.lease_owner is None and recovered_node.lease_expires_at is None
    async with session_factory() as session:
        recovery_events = await SqlAlchemyWorkflowRepository(session).events(run.id)
    assert [event["event"] for event in recovery_events[-2:]] == ["run.recovering", "run.recovered"]


@pytest.mark.asyncio
async def test_budget_blocked_checkpoint_never_commits_a_partial_version(session_factory):
    suffix = uuid4().hex
    project_id = f"budget-project-{suffix}"
    chapter_id = f"budget-chapter-{suffix}"
    version_id = f"base-version-{suffix}"
    async with session_factory() as session:
        session.add(
            ProjectModel(
                id=project_id,
                owner_id="budget-user",
                slug=f"budget-{suffix}",
                title="Budget",
            )
        )
        # Flush between FK levels: the ORM does not order inserts by
        # table-level FK dependencies, so each parent must hit the database
        # before its children (same reasoning as projects.add).
        await session.flush()
        session.add(
            ChapterModel(
                id=chapter_id,
                project_id=project_id,
                chapter_no=1,
                title="Budget chapter",
                status="DRAFT",
                active_version_id=version_id,
            )
        )
        await session.flush()
        session.add(
            ChapterVersionModel(
                id=version_id,
                chapter_id=chapter_id,
                version_no=1,
                content="Canonical manuscript",
                content_hash=hashlib.sha256(b"Canonical manuscript").hexdigest(),
                word_count=2,
            )
        )
        await session.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        definition = await WorkflowDefinitionService(uow).create(
            project_id,
            "budget-user",
            f"budget-flow-{suffix}",
            {
                "nodes": [
                    {
                        "id": "draft",
                        "kind": "write",
                        "title": "Draft",
                        "config": {
                            "chapter_id": chapter_id,
                            "reserved_tokens": 20,
                        },
                    }
                ],
                "edges": [],
            },
        )
        run, nodes = await WorkflowRunService(uow).create(
            definition.id,
            "budget-user",
            token_limit=10,
        )
        await uow.commit()

    async with session_factory() as session:
        versions = list(
            await session.scalars(
                select(ChapterVersionModel)
                .where(ChapterVersionModel.chapter_id == chapter_id)
                .order_by(ChapterVersionModel.version_no)
            )
        )
        persisted_node = await session.get(WorkflowNodeStateModel, nodes[0].id)
        persisted_run = await SqlAlchemyWorkflowRepository(session).get_owned(run.id, "budget-user")

    assert persisted_run is not None and persisted_run.status == "BUDGET_BLOCKED"
    assert persisted_node is not None and persisted_node.status == "BLOCKED"
    assert persisted_node.reserved_tokens == 20
    assert [(version.version_no, version.content) for version in versions] == [(1, "Canonical manuscript")]
