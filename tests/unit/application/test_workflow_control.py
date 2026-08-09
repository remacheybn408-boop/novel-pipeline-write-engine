import json
from types import SimpleNamespace

import pytest

from proseforge.application.workflows.control import WorkflowControlService
from proseforge.workflows.celery_app import should_abort_workflow


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


@pytest.mark.asyncio
async def test_resume_requeues_the_durable_workflow_command():
    command = {"user_id": "user-1", "chapter_numbers": [1], "provider": "openai", "model": "m", "editor_model": "m"}
    run = SimpleNamespace(id="run-1", status="PAUSED", checkpoint=json.dumps({"command": command}))
    uow = FakeUow(run)
    queue = FakeQueue()

    result = await WorkflowControlService(uow, queue).execute("run-1", "user-1", "resume")

    assert result.status == "QUEUED"
    assert uow.workflows.transitions == ["QUEUED"]
    assert queue.calls == [("proseforge.workflows.generate_novel", {"workflow_id": "run-1", **command})]
    assert run.checkpoint and "celery-task-1" in run.checkpoint
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_retry_requeues_failed_workflow():
    command = {"user_id": "user-1", "chapter_numbers": [2], "provider": "openai", "model": "m", "editor_model": "m"}
    run = SimpleNamespace(id="run-2", status="FAILED", checkpoint=json.dumps({"command": command}))
    uow = FakeUow(run)
    queue = FakeQueue()

    await WorkflowControlService(uow, queue).execute("run-2", "user-1", "retry")

    assert uow.workflows.transitions == ["QUEUED"]
    assert len(queue.calls) == 1


def test_worker_stops_at_cancel_safe_points():
    assert should_abort_workflow("CANCELLED") is True
    assert should_abort_workflow("RUNNING") is False
