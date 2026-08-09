"""Scene-pack budget accounting: CompileChatContext pre-deduction and
trim_scene_pack ordering."""

from __future__ import annotations

import pytest

from proseforge.application.conversations.compile_chat_context import CompileChatContext
from proseforge.application.work.retriever import (
    _estimate_tokens,
    render_pack_text,
    trim_scene_pack,
)
from proseforge.domain.conversation.entity import Message
from proseforge.domain.model.capabilities import ModelCapabilities


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self):
        self.added = []

    async def scalars(self, statement):
        return FakeScalars([])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


class FakeOutlines:
    async def list_owned(self, project_id, owner_id):
        return []


class FakeUow:
    def __init__(self):
        self.session = FakeSession()
        self.outlines = FakeOutlines()


def _capabilities(context_window: int, max_output_tokens: int) -> ModelCapabilities:
    return ModelCapabilities(context_window, max_output_tokens, False, None, False, False, "catalog")


def _message(message_id: str, role: str, content: str) -> Message:
    return Message(id=message_id, branch_id="b1", role=role, content=content)


@pytest.mark.asyncio
async def test_scene_pack_pre_deducted_from_history_allowance():
    history = [
        _message("old", "user", "古" * 400),
        _message("mid", "assistant", "古" * 400),
        _message("new", "user", "最新的一条消息"),
    ]
    capabilities = _capabilities(context_window=900, max_output_tokens=100)

    without_pack = await CompileChatContext(FakeUow()).execute(
        project_id="p1", history=history, capabilities=capabilities,
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
    )
    pack_text = "[世界观与设定]\n" + "设" * 300
    with_pack = await CompileChatContext(FakeUow()).execute(
        project_id="p1", history=history, capabilities=capabilities,
        provider="openai", model="gpt-test", reasoning={"level": "auto", "parameter": None}, user_id="u1",
        scene_pack_text=pack_text,
    )

    pack_blocks = [block for block in with_pack.system_blocks if block["type"] == "scene_pack"]
    assert len(pack_blocks) == 1 and pack_blocks[0]["text"] == pack_text
    assert pack_blocks[0]["priority"] == 95
    # The pack's cost comes out of the history allowance: fewer (or equal)
    # history messages survive than without the pack.
    assert len(with_pack.messages) <= len(without_pack.messages)
    # Total system tokens never exceed the input budget.
    system_tokens = sum(len(str(block["text"])) // 2 for block in with_pack.system_blocks)
    message_tokens = sum(len(block["text"]) // 2 for block in with_pack.messages)
    budget_input = 900 - 100 - 90  # window - output reserve - safety margin
    assert system_tokens + message_tokens <= budget_input + 200  # estimate slack


def test_trim_scene_pack_drops_evidence_first():
    sections = {
        "worldview": "世界观条目",
        "current_state": "当前状态",
        "constraints": "写作约束",
        "evidence": "\n\n".join(f"【第{i}章】证据块" for i in range(5)),
    }
    full = _estimate_tokens(render_pack_text(sections))
    budget = full - 10  # shave a little: only evidence may shrink
    trimmed = trim_scene_pack(sections, budget)
    assert "世界观条目" in trimmed and "写作约束" in trimmed
    assert trimmed.count("证据块") < 5
    assert _estimate_tokens(trimmed) <= budget


def test_trim_scene_pack_drops_sections_in_reverse_priority():
    sections = {
        "worldview": "世" * 200,
        "current_state": "状" * 200,
        "constraints": "约" * 200,
        "evidence": "证" * 200,
    }
    trimmed = trim_scene_pack(sections, budget_tokens=150)
    # evidence/constraints/current_state sacrificed before worldview.
    assert "证" not in trimmed
    assert "世" in trimmed
    assert _estimate_tokens(trimmed) <= 150 + 20


def test_trim_scene_pack_hard_truncates_worldview_as_last_resort():
    sections = {"worldview": "世" * 2000, "current_state": "", "constraints": "", "evidence": ""}
    trimmed = trim_scene_pack(sections, budget_tokens=100)
    assert _estimate_tokens(trimmed) <= 100 + 20
    assert len(trimmed) < 2000
