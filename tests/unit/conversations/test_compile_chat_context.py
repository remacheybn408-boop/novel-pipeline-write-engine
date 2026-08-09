from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from proseforge.application.conversations.compile_chat_context import CompileChatContext
from proseforge.domain.conversation.entity import Message
from proseforge.domain.model.capabilities import ModelCapabilities
from proseforge.infrastructure.database.models.remaining import (
    ContextSnapshotModel,
    OutlineModel,
)
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, fact_rows):
        self._fact_rows = fact_rows
        self.added = []

    async def scalars(self, statement):
        return FakeScalars(self._fact_rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


class FakeOutlines:
    def __init__(self, outlines):
        self._outlines = outlines

    async def list_owned(self, project_id, owner_id):
        return self._outlines


class FakeUow:
    def __init__(self, facts=None, outlines=None):
        self.session = FakeSession(facts or [])
        self.outlines = FakeOutlines(outlines or [])


def _fact(fact_id: str, key: str, *, pinned: bool = True, kind: str = "character") -> StoryBibleEntryModel:
    now = datetime.now(UTC)
    return StoryBibleEntryModel(
        id=fact_id, project_id="p1", kind=kind, key=key, value_json=json.dumps({"note": f"note-{key}", "triggers": [key], "budget_tokens": 24}),
        status="active", confidence=1.0, source="user", pinned=pinned, version=1, created_at=now, updated_at=now,
    )


def _capabilities(context_window: int = 8192, max_output_tokens: int = 1024) -> ModelCapabilities:
    return ModelCapabilities(context_window, max_output_tokens, False, None, False, False, "catalog")


def _message(message_id: str, role: str, content: str) -> Message:
    return Message(id=message_id, branch_id="b1", role=role, content=content)


@pytest.mark.asyncio
async def test_pinned_facts_are_injected_into_system_blocks():
    uow = FakeUow(facts=[_fact("f1", "hero"), _fact("f2", "villain")])
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=[_message("m1", "user", "hello")], capabilities=_capabilities(),
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
    )
    fact_blocks = [block for block in context.system_blocks if block["type"] == "story_fact"]
    assert len(fact_blocks) == 2
    assert any("hero" in block["text"] for block in fact_blocks)
    assert context.injected_fact_ids == ("f1", "f2")
    assert any(block["type"] == "persona" for block in context.system_blocks)


@pytest.mark.asyncio
async def test_oldest_messages_are_trimmed_first_when_over_budget():
    history = [
        _message("old", "user", "古" * 400),
        _message("mid", "assistant", "古" * 400),
        _message("new", "user", "最新的一条消息"),
    ]
    capabilities = _capabilities(context_window=900, max_output_tokens=100)
    uow = FakeUow()
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=history, capabilities=capabilities,
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
    )
    texts = [block["text"] for block in context.messages]
    assert texts[-1] == "最新的一条消息"
    assert len(texts) < 3  # oldest history is dropped first to fit the budget
    snapshot = next(row for row in uow.session.added if isinstance(row, ContextSnapshotModel))
    payload = json.loads(snapshot.payload)
    omitted_ids = [item["message_id"] for item in payload["omitted"]]
    assert "old" in omitted_ids
    assert all(item["reason"] == "budget_trim" for item in payload["omitted"])
    kept_ids = [message.id for message in history if message.id not in omitted_ids and message.content]
    assert payload["message_ids"] == kept_ids


@pytest.mark.asyncio
async def test_snapshot_persists_blocks_injected_ids_and_omitted():
    outline = OutlineModel(
        id="o1", project_id="p1", title="大纲", status="CONFIRMED",
        payload=json.dumps({"title": "大纲", "raw_content": "第一卷：风起"}), missing_questions="[]", confirmed=True,
    )
    uow = FakeUow(facts=[_fact("f9", "hero")], outlines=[outline])
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=[_message("m1", "user", "继续"), _message("m2", "assistant", "好的")],
        capabilities=_capabilities(), provider="openai", model="gpt-test",
        reasoning={"level": "deep", "parameter": "effort", "strength": 0.75}, user_id="u1",
    )
    assert context.snapshot_id
    snapshot = next(row for row in uow.session.added if isinstance(row, ContextSnapshotModel))
    assert snapshot.id == context.snapshot_id
    assert snapshot.project_id == "p1"
    payload = json.loads(snapshot.payload)
    assert payload["injected_fact_ids"] == ["f9"]
    assert "omitted" in payload
    block_types = {block["type"] for block in payload["blocks"]}
    assert {"persona", "outline", "story_fact"} <= block_types
    assert context.model_snapshot["provider"] == "openai"
    assert context.model_snapshot["model"] == "gpt-test"
    assert context.model_snapshot["source"] == "catalog"
    assert context.model_snapshot["context_snapshot_id"] == context.snapshot_id
    assert context.reasoning_snapshot["level"] == "deep"
    assert [message["role"] for message in context.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_unsupported_reasoning_snapshot_is_carried_through():
    uow = FakeUow()
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=[_message("m1", "user", "hi")], capabilities=_capabilities(),
        provider="openai", model="gpt-test",
        reasoning={"level": "deep", "supported": False, "reason": "unsupported"}, user_id="u1",
    )
    assert context.reasoning_snapshot == {"level": "deep", "supported": False, "reason": "unsupported"}


@pytest.mark.asyncio
async def test_triggered_facts_are_injected_but_unmatched_facts_do_not_consume_budget():
    uow = FakeUow(facts=[_fact("hit", "Mira", pinned=False), _fact("miss", "unrelated", pinned=False)])
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=[_message("m1", "user", "Continue Mira's scene")], capabilities=_capabilities(),
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
    )
    fact_blocks = [block for block in context.system_blocks if block["type"] == "story_fact"]
    assert [block["fact_id"] for block in fact_blocks] == ["hit"]
    snapshot = next(row for row in uow.session.added if isinstance(row, ContextSnapshotModel))
    payload = json.loads(snapshot.payload)
    assert payload["injected_fact_ids"] == ["hit"]
    assert any(item["source_id"] == "miss" and item["reason"] == "not_triggered" for item in payload["omitted"])


@pytest.mark.asyncio
async def test_pinned_fact_survives_budget_pressure_before_triggered_fact():
    pinned = _fact("pinned", "canon", pinned=True)
    pinned.value_json = json.dumps({"note": "pinned canon", "triggers": [], "budget_tokens": 900})
    triggered = _fact("triggered", "Mira", pinned=False)
    triggered.value_json = json.dumps({"note": "triggered", "triggers": ["Mira"], "budget_tokens": 50})
    uow = FakeUow(facts=[pinned, triggered])
    context = await CompileChatContext(uow).execute(
        project_id="p1", history=[_message("m1", "user", "Mira arrives")], capabilities=_capabilities(context_window=700, max_output_tokens=100),
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
    )
    assert context.injected_fact_ids == ("pinned",)
