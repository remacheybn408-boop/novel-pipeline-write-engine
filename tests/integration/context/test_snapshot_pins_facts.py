from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from proseforge.application.context.build_snapshot import BuildContextSnapshot
from proseforge.context_engine.budgeting import calculate_budget
from proseforge.domain.conversation.entity import Conversation
from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.repositories.context import (
    SqlAlchemyContextRepository,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork


@pytest.mark.asyncio
async def test_snapshot_is_reproducible_and_linked_from_generation_message(session_factory):
    builder = BuildContextSnapshot()
    budget = calculate_budget(2_000, 200)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        project = Project.create(owner_id="owner", slug="snapshot-facts", title="Snapshot facts")
        await uow.projects.add(project)
        now = datetime.now(UTC)
        uow.session.add_all([
            StoryBibleEntryModel(id="pin", project_id=project.id, kind="world_rule", key="canon", value_json=json.dumps({"triggers": [], "budget_tokens": 40}), pinned=True, status="active", confidence=1.0, source="user", version=1, created_at=now, updated_at=now),
            StoryBibleEntryModel(id="hit", project_id=project.id, kind="character", key="Mira", value_json=json.dumps({"triggers": ["Mira"], "budget_tokens": 40}), pinned=False, status="active", confidence=0.7, source="user", version=1, created_at=now, updated_at=now),
            StoryBibleEntryModel(id="miss", project_id=project.id, kind="character", key="Ilan", value_json=json.dumps({"triggers": ["Ilan"], "budget_tokens": 40}), pinned=False, status="active", confidence=0.1, source="user", version=1, created_at=now, updated_at=now),
        ])
        rows = list((await uow.session.scalars(select(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == project.id).order_by(StoryBibleEntryModel.id))).all())
        selection = builder.select_story_facts(rows, "Mira enters", budget.input_tokens)
        blocks = [builder.describe_block(block_type="persona", source_id="default", text="writer"), *selection.blocks]
        first = builder.persist(uow.session, project_id=project.id, blocks=blocks, messages=[], injected_fact_ids=selection.injected_fact_ids, omitted=selection.omitted, budget=budget)
        second = builder.persist(uow.session, project_id=project.id, blocks=blocks, messages=[], injected_fact_ids=selection.injected_fact_ids, omitted=selection.omitted, budget=budget)
        conversation = Conversation.create(project.id)
        branch = await uow.conversations.create(conversation)
        message = await uow.conversations.append_message(branch.id, "assistant", "draft")
        await uow.conversations.set_message_snapshots(message.id, {"context_snapshot_id": first.id}, {"level": "auto"})
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await SqlAlchemyContextRepository(uow.session).get_snapshot_owned(first.id, "owner")
        message = await uow.conversations.get_message(message.id)
        assert first.snapshot_hash == second.snapshot_hash
        assert stored is not None
        assert json.loads(stored.payload)["injected_fact_ids"] == ["pin", "hit"]
        assert any(item["source_id"] == "miss" and item["reason"] == "not_triggered" for item in json.loads(stored.payload)["omitted"])
        assert message is not None and message.model_snapshot == {"context_snapshot_id": first.id}
