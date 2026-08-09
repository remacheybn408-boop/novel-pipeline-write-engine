"""Agent-run control endpoints: POST /api/v3/agent-runs/{id}/retry|resume.

H4 regression: a run-level retry (no task_id) must reset every FAILED task
back to PENDING, otherwise the executor sees "no PENDING, some FAILED" and
flips the run straight back to FAILED — the retry button was dead.

M1 regression: when the queue rejects the enqueue AFTER the status commit,
the run must be rolled back to its pre-control status (FAILED/PAUSED) and
the API must answer 503 QUEUE_UNAVAILABLE — never wedge in RUNNING.

L4 regression: resume is only valid from PAUSED (a PENDING run is already
queued; resuming it would double-enqueue), and resume/retry re-check the
per-user concurrency cap so a pause/resume cycle cannot bypass
MAX_ACTIVE_RUNS_PER_USER.

Real app on native sqlite (TestClient + lifespan).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        yield test_client


def _start_run(client: TestClient, slug: str = "novel-1") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    project_id = response.json()["id"]
    response = client.post(f"/api/v3/projects/{project_id}/agent-runs", json={"goal": "写第三章"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set_run_state(client: TestClient, run_id: str, run_status: str, task_states: dict[str, tuple[str, str | None]]) -> None:
    """Force the run row and per-task_key (status, last_error) directly —
    the API has no path that lands a run in FAILED with failed tasks."""

    async def _update() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.status = run_status
            for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id)):
                if task.task_key in task_states:
                    task.status, task.last_error = task_states[task.task_key]
            await uow.commit()

    asyncio.run(_update())


def _read_state(client: TestClient, run_id: str) -> tuple[str, dict[str, tuple[str, int, str | None]]]:
    """Return (run_status, {task_key: (status, attempts, last_error)})."""

    async def _read() -> tuple[str, dict[str, tuple[str, int, str | None]]]:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            tasks = {
                task.task_key: (task.status, task.attempts, task.last_error)
                for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id))
            }
            return run.status, tasks

    return asyncio.run(_read())


def test_run_level_retry_resets_failed_tasks_to_pending(client: TestClient):
    run_id = _start_run(client)
    _set_run_state(client, run_id, "FAILED", {"planner": ("FAILED", "boom"), "reviewer": ("PENDING", None)})

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RUNNING"
    run_status, tasks = _read_state(client, run_id)
    assert run_status == "RUNNING"
    # The FAILED task is re-queued with a FRESH rescue cycle (attempts reset
    # to 0 — the old attempts+1 left the task at the retry ceiling, so the
    # first failure after retry flipped the run straight back to FAILED);
    # the untouched PENDING task stays as-is.
    assert tasks["planner"] == ("PENDING", 0, None)
    assert tasks["reviewer"] == ("PENDING", 0, None)


def test_retry_enqueue_failure_rolls_back_to_failed(client: TestClient, monkeypatch):
    run_id = _start_run(client)
    _set_run_state(client, run_id, "FAILED", {"planner": ("FAILED", "boom"), "reviewer": ("PENDING", None)})

    async def _broken_enqueue(task_name, payload):
        raise RuntimeError("queue down")

    monkeypatch.setattr(client.app.state.queue, "enqueue", _broken_enqueue)

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "QUEUE_UNAVAILABLE"
    assert error["retryable"] is True
    # The run must not wedge in RUNNING: it goes back to FAILED, retryable.
    run_status, _tasks = _read_state(client, run_id)
    assert run_status == "FAILED"


def test_resume_enqueue_failure_rolls_back_to_paused(client: TestClient, monkeypatch):
    run_id = _start_run(client)
    _set_run_state(client, run_id, "PAUSED", {})

    async def _broken_enqueue(task_name, payload):
        raise RuntimeError("queue down")

    monkeypatch.setattr(client.app.state.queue, "enqueue", _broken_enqueue)

    response = client.post(f"/api/v3/agent-runs/{run_id}/resume")

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    run_status, _tasks = _read_state(client, run_id)
    assert run_status == "PAUSED"


def test_resume_of_pending_run_is_rejected(client: TestClient):
    # A PENDING run is already queued; resuming it would double-enqueue the
    # same run with two executors claiming it.
    run_id = _start_run(client)

    response = client.post(f"/api/v3/agent-runs/{run_id}/resume")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INVALID_RUN_TRANSITION"
    run_status, _tasks = _read_state(client, run_id)
    assert run_status == "PENDING"


def test_resume_rejected_when_concurrency_limit_reached(client: TestClient):
    # One paused run + three active runs: resuming the paused one would push
    # the user past MAX_ACTIVE_RUNS_PER_USER, so it must be denied.
    run_id = _start_run(client)
    response = client.post(f"/api/v3/agent-runs/{run_id}/pause")
    assert response.status_code == 200, response.text
    for index in range(3):
        _start_run(client, slug=f"novel-{index + 2}")

    response = client.post(f"/api/v3/agent-runs/{run_id}/resume")

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "RUN_CONCURRENCY_LIMIT"
    assert error["retryable"] is True
    run_status, _tasks = _read_state(client, run_id)
    assert run_status == "PAUSED"


def test_retry_rejected_when_concurrency_limit_reached(client: TestClient):
    # Same cap re-check on the retry path: a failed run must not jump the
    # queue while three other runs are active.
    run_id = _start_run(client)
    _set_run_state(client, run_id, "FAILED", {"planner": ("FAILED", "boom"), "reviewer": ("PENDING", None)})
    for index in range(3):
        _start_run(client, slug=f"novel-{index + 2}")

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "RUN_CONCURRENCY_LIMIT"
    assert error["retryable"] is True
    run_status, tasks = _read_state(client, run_id)
    assert run_status == "FAILED"
    # Nothing was reset: the failed task keeps its error and attempt count.
    assert tasks["planner"] == ("FAILED", 0, "boom")


def test_run_level_retry_clears_attempt_counters_and_backoff(client: TestClient):
    """Run 级 retry 开启新一次脱困周期：attempts / retryable_attempts 清零、
    退避排程清空——重试的任务重新拥有完整的 DEFAULT_MAX_ATTEMPTS 与可重试
    额度，而不是首次失败即触顶。"""
    run_id = _start_run(client)
    _set_run_state(client, run_id, "FAILED", {"planner": ("FAILED", "boom")})

    async def _set_counters() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            task = await uow.session.scalar(
                select(AgentTaskModel).where(AgentTaskModel.run_id == run_id, AgentTaskModel.task_key == "planner")
            )
            task.attempts = 4
            task.retryable_attempts = 2
            task.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
            await uow.commit()

    asyncio.run(_set_counters())

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")
    assert response.status_code == 200, response.text

    async def _read_counters() -> tuple[int, int, object]:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            task = await uow.session.scalar(
                select(AgentTaskModel).where(AgentTaskModel.run_id == run_id, AgentTaskModel.task_key == "planner")
            )
            return task.attempts, task.retryable_attempts, task.next_attempt_at

    assert asyncio.run(_read_counters()) == (0, 0, None)


def test_budget_exhausted_retry_raises_budget_limit_with_audit(client: TestClient):
    """BUDGET_EXHAUSTED retry：budget_used 保持记账语义不回零，budget_limit
    ×1.5 取整上调（不低于 used+1）保证重跑有空间；run.retry 审计事件记录
    预算调整。旧行为只翻转状态，重跑必然在同一任务再次熔断。"""
    run_id = _start_run(client)
    _set_run_state(client, run_id, "BUDGET_EXHAUSTED", {"planner": ("FAILED", "budget exhausted")})

    async def _set_budget() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.budget_limit = 1000
            run.budget_used = 990
            run.terminal_reason = "task token budget exceeds remaining run budget"
            await uow.commit()

    asyncio.run(_set_budget())

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RUNNING"
    assert response.json()["budget_limit"] == 1500
    assert response.json()["budget_used"] == 990

    async def _read_events() -> list[dict[str, object]]:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            return [
                json.loads(row.payload)
                for row in await uow.session.scalars(
                    select(AgentEventModel)
                    .where(AgentEventModel.run_id == run_id, AgentEventModel.event_type == "run.retry")
                    .order_by(AgentEventModel.sequence)
                )
            ]

    retry_events = asyncio.run(_read_events())
    assert retry_events[-1]["budget_adjustment"] == {"old_limit": 1000, "new_limit": 1500, "budget_used": 990}


def test_failed_run_retry_does_not_touch_budget(client: TestClient):
    """非熔断 retry 不动预算：budget_adjustment 只出现在 BUDGET_EXHAUSTED 脱困。"""
    run_id = _start_run(client)
    _set_run_state(client, run_id, "FAILED", {"planner": ("FAILED", "boom")})

    async def _set_budget() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.budget_limit = 1000
            run.budget_used = 500
            await uow.commit()

    asyncio.run(_set_budget())

    response = client.post(f"/api/v3/agent-runs/{run_id}/retry")

    assert response.status_code == 200, response.text
    assert response.json()["budget_limit"] == 1000
    assert response.json()["budget_used"] == 500
