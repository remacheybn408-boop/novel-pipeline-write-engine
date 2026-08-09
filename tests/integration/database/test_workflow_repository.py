from datetime import UTC, datetime, timedelta

import pytest

from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.repositories.workflow import (
    SqlAlchemyWorkflowRepository,
)


@pytest.mark.asyncio
async def test_workflow_events_and_transitions_are_durable(session_factory):
    async with session_factory() as session:
        session.add(ProjectModel(id="p-workflow", owner_id="u1", slug="workflow", title="Workflow"))
        await session.flush()
        repository = SqlAlchemyWorkflowRepository(session)
        run = await repository.create("p-workflow", "NOVEL")
        await repository.set_command(run, {"user_id": "u1", "chapter_numbers": [1], "provider": "openai", "model": "m", "editor_model": "m"})
        await repository.transition(run, "RUNNING")
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyWorkflowRepository(session)
        owned = await repository.get_owned(run.id, "u1")
        events = await repository.events(run.id)
        replayed = await repository.events(run.id, 1)
        command = await repository.get_command(owned)

    assert owned is not None and owned.status == "RUNNING"
    assert [event["event"] for event in events] == ["QUEUED", "RUNNING"]
    assert [event["event"] for event in replayed] == ["RUNNING"]
    assert command == {"user_id": "u1", "chapter_numbers": [1], "provider": "openai", "model": "m", "editor_model": "m"}


@pytest.mark.asyncio
async def test_workflow_lease_checkpoint_recovery_and_cost_guard(session_factory):
    async with session_factory() as session:
        session.add(ProjectModel(id="p-recovery", owner_id="u1", slug="recovery", title="Recovery"))
        await session.flush()
        repository = SqlAlchemyWorkflowRepository(session)
        run = await repository.create("p-recovery", "NOVEL", cost_limit=5)
        assert await repository.acquire_lease(run, "worker-a", ttl_seconds=60)
        assert not await repository.acquire_lease(run, "worker-b", ttl_seconds=60)
        await repository.checkpoint(run, "worker-a", "chapter:1", estimated_cost=3)
        with pytest.raises(ValueError, match="cost limit"):
            await repository.checkpoint(run, "worker-a", "chapter:2", estimated_cost=3)
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        run.status = "RUNNING"
        await session.commit()
        assert await repository.recover_expired() == 1
        await session.commit()
        assert run.status == "RECOVERING"
