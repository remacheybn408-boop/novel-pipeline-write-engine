"""V1 control Idempotency-Key semantics (V2-008).

Same contract as v2: a duplicate key returns the first result and repeats no
side effects (no extra transition, no extra enqueue).  Fakes mirror
tests/unit/application/test_workflow_control.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from proseforge.application.workflows.control import WorkflowControlService

COMMAND = {"user_id": "user-1", "chapter_numbers": [1], "provider": "openai", "model": "m", "editor_model": "m"}


class FakeWorkflowRepository:
    def __init__(self, run):
        self.run = run
        self.transitions = []

    async def get_owned(self, workflow_id, user_id, *, lock=False):
        # lock=True is SELECT ... FOR UPDATE in the real repo; the in-memory
        # fake has no concurrent readers, so the flag changes nothing here.
        return self.run if workflow_id == self.run.id and user_id == "user-1" else None

    async def transition(self, run, status):
        self.transitions.append(status)
        run.status = status

    async def get_command(self, run):
        return json.loads(run.checkpoint)["command"]

    async def set_task(self, run, task_id):
        document = json.loads(run.checkpoint)
        document["active_task_id"] = task_id
        run.checkpoint = json.dumps(document)


class FakeUow:
    def __init__(self, run):
        self.workflows = FakeWorkflowRepository(run)
        self.commits = 0

    async def commit(self):
        self.commits += 1


class FakeQueue:
    def __init__(self):
        self.calls = []

    async def enqueue(self, task_name, payload):
        self.calls.append((task_name, payload))
        return "celery-task-1"


def _run(status: str = "RUNNING") -> SimpleNamespace:
    return SimpleNamespace(id="run-1", status=status, checkpoint=json.dumps({"command": COMMAND}))


@pytest.mark.asyncio
async def test_duplicate_pause_key_replays_first_result_without_side_effects():
    run = _run()
    uow = FakeUow(run)

    first = await WorkflowControlService(uow, FakeQueue()).execute("run-1", "user-1", "pause", idempotency_key="k-1")
    assert first.status == "PAUSED"
    assert first.idempotent_replay is False
    assert uow.commits == 1

    replay = await WorkflowControlService(uow, FakeQueue()).execute("run-1", "user-1", "pause", idempotency_key="k-1")
    assert replay.status == "PAUSED"
    assert replay.idempotent_replay is True
    assert uow.workflows.transitions == ["PAUSED"]
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_duplicate_resume_key_returns_first_task_without_reenqueue():
    run = _run("PAUSED")
    uow = FakeUow(run)
    queue = FakeQueue()

    first = await WorkflowControlService(uow, queue).execute("run-1", "user-1", "resume", idempotency_key="k-2")
    assert first.task_id == "celery-task-1"
    assert uow.commits == 2

    replay = await WorkflowControlService(uow, queue).execute("run-1", "user-1", "resume", idempotency_key="k-2")
    assert replay.idempotent_replay is True
    assert replay.task_id == "celery-task-1"
    assert len(queue.calls) == 1
    assert uow.workflows.transitions == ["QUEUED"]
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_competing_key_is_a_new_command_and_conflicts():
    run = _run()
    uow = FakeUow(run)
    service = WorkflowControlService(uow, FakeQueue())

    await service.execute("run-1", "user-1", "pause", idempotency_key="k-1")
    with pytest.raises(ValueError, match="invalid workflow transition"):
        await service.execute("run-1", "user-1", "pause", idempotency_key="k-other")


@pytest.mark.asyncio
async def test_missing_key_keeps_legacy_non_idempotent_behavior():
    run = _run()
    uow = FakeUow(run)
    service = WorkflowControlService(uow, FakeQueue())

    await service.execute("run-1", "user-1", "pause")
    with pytest.raises(ValueError, match="invalid workflow transition"):
        await service.execute("run-1", "user-1", "pause")
    assert "control_results" not in json.loads(run.checkpoint)
