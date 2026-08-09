"""default_role_handler: over-budget dropped blocks are compacted, not lost.

Trim priority is unchanged (style_card -> scene_pack -> artifacts ->
memory_slice), but the popped style-card/artifact/memory blocks are folded
into one validated context_engine summary block (context.compacted audit
event) sized to the remaining budget. A summary-validation failure falls
back to the hard trim. Fake provider only — no DB, no network.
"""

from __future__ import annotations

import pytest

from proseforge.application.agents.role_handlers import (
    _compact_dropped_blocks,
    default_role_handler,
)
from proseforge.domain.ports.model_provider import GenerationEvent


class _FakeProvider:
    provider_id = "fake"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationEvent("content.delta", text='{"summary": "ok"}')
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})


def _context(provider, **overrides):
    context = {
        "task": {"role": "scene_writer", "task_key": "t1", "token_budget": 1},
        "run": {"id": "r1", "goal": "写第三章", "goal_hash": "h", "memory_slice": []},
        "provider": provider,
        "provider_id": "fake",
        "model": "fake-model",
        "uow_factory": None,
        "artifacts": [],
        "scene_pack": None,
        "input_budget": None,
    }
    context.update(overrides)
    return context


@pytest.mark.asyncio
async def test_over_budget_dropped_blocks_compacted_into_summary():
    provider = _FakeProvider()
    artifacts = [{"id": f"a{i}", "artifact_type": "candidate", "task_key": f"k{i}", "preview": "预" * 3000} for i in range(4)]
    memory = [{"fact_key": f"f{i}", "value": "记" * 3000} for i in range(4)]

    result = await default_role_handler(_context(
        provider, artifacts=artifacts,
        run={"id": "r1", "goal": "g", "goal_hash": "h", "memory_slice": memory},
        input_budget=4000,  # fits the base prompt, not 8 x 3K-char blocks
    ))

    prompt = provider.requests[0].input_blocks[0]["text"]
    trimmed = [event for event in result.extra_events if event.get("event") == "context.trimmed"]
    compacted = [event for event in result.extra_events if event.get("event") == "context.compacted"]
    assert trimmed and trimmed[0]["kinds"] == ["style_card", "artifacts", "memory_slice"]
    assert compacted, "dropped blocks must be compacted, not silently lost"
    assert compacted[0]["validation"] == "PASS"
    assert compacted[0]["kinds"] == ["artifacts", "memory_slice", "style_card"]  # 压缩事件 kinds 为排序集合
    assert compacted[0]["blocks"] >= 1
    # The summary block rides the prompt, sized to the remaining budget.
    assert "[上下文压缩摘要" in prompt
    assert len(prompt) // 2 <= 4000 + 50  # estimate slack


@pytest.mark.asyncio
async def test_compaction_validation_failure_falls_back_to_hard_trim(monkeypatch):
    from proseforge.context_engine import compaction as compaction_module

    class _BlockedValidation:
        status = "BLOCK"
        errors = ("unknown_source_message",)

    class _BlockedResult:
        def __init__(self):
            self.summary = {"facts": []}
            self.validation = _BlockedValidation()

    monkeypatch.setattr(compaction_module, "compact_reversibly", lambda blocks, summary=None: _BlockedResult())
    provider = _FakeProvider()
    artifacts = [{"id": f"a{i}", "artifact_type": "candidate", "task_key": f"k{i}", "preview": "预" * 3000} for i in range(4)]

    result = await default_role_handler(_context(provider, artifacts=artifacts, input_budget=4000))

    prompt = provider.requests[0].input_blocks[0]["text"]
    assert "[上下文压缩摘要" not in prompt  # hard trim kept
    assert not [event for event in result.extra_events if event.get("event") == "context.compacted"]
    assert [event for event in result.extra_events if event.get("event") == "context.trimmed"]


@pytest.mark.asyncio
async def test_no_room_after_trim_keeps_hard_trim():
    provider = _FakeProvider()
    artifacts = [{"id": f"a{i}", "artifact_type": "candidate", "task_key": f"k{i}", "preview": "预" * 3000} for i in range(4)]

    result = await default_role_handler(_context(provider, artifacts=artifacts, input_budget=10))

    assert not [event for event in result.extra_events if event.get("event") == "context.compacted"]
    assert "[上下文压缩摘要" not in provider.requests[0].input_blocks[0]["text"]


def test_compact_summary_block_capped_to_remaining_budget():
    blocks = [{"id": f"a{i}", "kind": "artifacts", "text": "x" * 500} for i in range(5)]

    compacted = _compact_dropped_blocks(blocks, max_chars=300)

    assert compacted is not None
    text, event = compacted
    assert len(text) <= 300
    assert event["blocks"] == 5
    assert event["kinds"] == ["artifacts"]
    assert event["validation"] == "PASS"


def test_compact_skipped_when_remaining_budget_too_small():
    assert _compact_dropped_blocks([{"id": "a", "kind": "artifacts", "text": "x" * 500}], max_chars=10) is None
