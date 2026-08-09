"""PostgreSQL-only regression for the agent-runs creation write sequence.

Production incident: start_run added agent_runs WITHOUT a flush, then added
agent_policy_snapshots / agent_tasks children; the SELECT FOR UPDATE inside
_event() triggered an autoflush, and with zero relationship() in the project
the ORM does not guarantee parent-before-child insert ordering. sqlite never
enforced the FK so every test stayed green; PostgreSQL rejected the commit
(fk_agent_policy_snapshots_run_id) and cluster mode was unusable.

This test drives the REAL start_run route function against a live
PostgreSQL (PROSEFORGE_TEST_DATABASE_URL), committing the full
run + snapshot + tasks + event sequence. It is skipped/errored without PG
like the rest of tests/integration/database.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from proseforge.api.routes.agent_runs import (
    AgentRunRequest,
    GraphTaskRequest,
    start_run,
)
from proseforge.application.auth.service import AuthUser
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentPolicySnapshotModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, task_name: str, payload: dict) -> None:
        self.enqueued.append((task_name, payload))


def _fake_request() -> SimpleNamespace:
    settings = SimpleNamespace(environment="development", master_key=SecretStr(MASTER_KEY))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, queue=_FakeQueue())),
        state=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_start_run_commits_run_snapshot_tasks_and_event(session_factory):
    async with session_factory() as session:
        session.add(ProjectModel(id="p-agent", owner_id="u1", slug="agent", title="Agent", mode="work"))
        await session.commit()

    user = AuthUser(id="u1", email="u1@example.local", role="ADMIN")
    payload = AgentRunRequest(
        goal="写出第三章的冲突升级",
        tasks=[
            GraphTaskRequest(id="planner", role="chief_planner", token_budget=10),
            GraphTaskRequest(id="reviewer", role="continuity_reviewer", depends_on=["planner"], token_budget=10),
        ],
        budget_limit=100,
    )
    uow = SqlAlchemyUnitOfWork(session_factory)
    request = _fake_request()

    response = await start_run("p-agent", payload, request, user, uow)

    run_id = str(response["id"])
    assert response["status"] == "PENDING"
    # The run was enqueued for the executor after a successful commit.
    assert request.app.state.queue.enqueued == [
        ("proseforge.agents.execute_run", {"run_id": run_id, "user_id": "u1", "provider": None, "model": None})
    ]

    # Read back in a fresh session: every FK child row must be durable.
    async with session_factory() as session:
        snapshot = await session.scalar(
            select(AgentPolicySnapshotModel).where(AgentPolicySnapshotModel.run_id == run_id)
        )
        tasks = list((await session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id))).all())
        events = list(
            (await session.scalars(
                select(AgentEventModel).where(AgentEventModel.run_id == run_id).order_by(AgentEventModel.sequence)
            )).all()
        )
    assert snapshot is not None and snapshot.policy_version == response["policy_version"]
    assert {task.task_key for task in tasks} == {"planner", "reviewer"}
    assert [event.event_type for event in events] == ["run.created"]
    assert events[0].sequence == 1
