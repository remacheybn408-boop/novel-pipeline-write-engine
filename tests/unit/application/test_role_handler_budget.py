"""default_role_handler: scene-pack injection and input-budget trimming.

Trim order when over budget: style card injection -> prev chapter text ->
scene_pack (retriever trim_scene_pack) -> artifact previews -> memory slice
entries; every trimmed kind lands in a context.trimmed extra event.
Fake provider only — no DB, no network.
"""

from __future__ import annotations

import pytest

from proseforge.application.agents.role_handlers import default_role_handler
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
async def test_scene_pack_injected_before_artifacts():
    provider = _FakeProvider()
    sections = {"worldview": "世界观：低魔", "evidence": "证据块"}
    artifacts = [{"id": "a1", "artifact_type": "candidate", "task_key": "p", "preview": "大纲预览"}]

    await default_role_handler(_context(provider, scene_pack=sections, artifacts=artifacts))

    prompt = provider.requests[0].input_blocks[0]["text"]
    assert "叙事检索场景包：" in prompt
    assert "世界观：低魔" in prompt
    assert prompt.index("叙事检索场景包：") < prompt.index("上游 Artifact 摘要：")


@pytest.mark.asyncio
async def test_no_scene_pack_no_section():
    provider = _FakeProvider()
    await default_role_handler(_context(provider))
    assert "叙事检索场景包" not in provider.requests[0].input_blocks[0]["text"]


@pytest.mark.asyncio
async def test_over_budget_trims_in_order_scene_pack_then_artifacts_then_memory():
    provider = _FakeProvider()
    sections = {"worldview": "世" * 4000, "evidence": "证" * 4000}
    artifacts = [{"id": f"a{i}", "artifact_type": "candidate", "task_key": f"k{i}", "preview": "预" * 200} for i in range(5)]
    memory = [{"fact_key": f"f{i}", "value": "记" * 200} for i in range(5)]

    result = await default_role_handler(_context(
        provider, scene_pack=sections, artifacts=artifacts,
        run={"id": "r1", "goal": "g", "goal_hash": "h", "memory_slice": memory},
        input_budget=10,  # deliberately tiny: everything must be trimmed
    ))

    prompt = provider.requests[0].input_blocks[0]["text"]
    events = [event for event in result.extra_events if event.get("event") == "context.trimmed"]
    # 文风技法卡（缺省回退卡也在场）最先让位，其后才是场景包/摘要/记忆
    assert events and events[0]["kinds"] == ["style_card", "scene_pack", "artifacts", "memory_slice"]
    # Pack trimmed (not dropped), previews/memory dropped entirely.
    assert len(prompt) < (len(sections["worldview"]) + len(sections["evidence"]))
    assert "预" * 50 not in prompt
    assert "记" * 50 not in prompt


@pytest.mark.asyncio
async def test_within_budget_trims_nothing():
    provider = _FakeProvider()
    sections = {"worldview": "短"}
    result = await default_role_handler(_context(provider, scene_pack=sections, input_budget=10_000_000))
    assert result.extra_events == []
    assert "短" in provider.requests[0].input_blocks[0]["text"]
