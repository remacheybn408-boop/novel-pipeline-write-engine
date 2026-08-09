"""write 图 promise_* 三节点装配测试（奥莉维亚·动态承诺）。

覆盖：write 图含 promise_contract/promise_verify/promise_register 三新节点且
拓扑合法；promise_ 前缀 task_key 不触门禁硬编码名单（review_*/merge/rewrite/
recheck/select/scene）；promise_keeper 席位映射 orchestrator（跟随总调度模型）；
专家 handler 注册生效；scene_writer 注入【本章承诺契约】块及缺失降级。
"""

from __future__ import annotations

import json

import pytest

from proseforge.application.agents.intent import graph_for_intent
from proseforge.application.agents.role_handlers import (
    default_role_handler,
    handler_for,
)
from proseforge.application.models.cluster_config import agent_role_to_cluster_role
from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph
from proseforge.domain.ports.model_provider import GenerationEvent

# 门禁硬编码 task_key 名单（workflows/agent_executor.py:267-377）：promise_*
# 前缀必须全部避开，否则会被门禁/终局逻辑误认领。
_GATE_EXACT_KEYS = {"merge", "rewrite", "recheck", "select", "scene"}
_GATE_PREFIXES = ("review_", "scene")


def test_write_graph_contains_three_promise_nodes():
    graph = graph_for_intent("write")
    by_id = {str(item["id"]): item for item in graph}
    assert by_id["promise_contract"]["role"] == "promise_keeper"
    assert by_id["promise_verify"]["role"] == "promise_keeper"
    assert by_id["promise_register"]["role"] == "promise_keeper"
    # 契约卡：character 后、scene_a/b/c/d 前
    assert by_id["promise_contract"]["depends_on"] == ["character"]
    for scene in ("scene_a", "scene_b", "scene_c", "scene_d"):
        assert "promise_contract" in by_id[scene]["depends_on"]
    # 核对：recheck 后；登记：图尾只依赖核对
    assert by_id["promise_verify"]["depends_on"] == ["recheck"]
    assert by_id["promise_register"]["depends_on"] == ["promise_verify"]


def test_write_graph_topology_stays_legal():
    graph = graph_for_intent("write")
    specs = tuple(AgentTaskSpec(id=str(item["id"]), role=str(item["role"]), depends_on=tuple(item["depends_on"])) for item in graph)
    order = TaskGraph(revision=1, tasks=specs).topological_order()
    assert len(order) == 17
    assert order.index("promise_contract") < min(order.index(key) for key in ("scene_a", "scene_b", "scene_c", "scene_d"))
    # 评审合议：三评审之后、merge 之前
    assert order.index("review_council") > max(order.index(key) for key in ("review_continuity", "review_adversarial", "review_style"))
    assert order.index("merge") > order.index("review_council")
    assert order.index("promise_verify") > order.index("recheck")
    assert order.index("promise_register") > order.index("promise_verify")


def test_promise_task_keys_avoid_gate_lists():
    for item in graph_for_intent("write"):
        task_key = str(item["id"])
        if not task_key.startswith("promise_"):
            continue
        assert task_key not in _GATE_EXACT_KEYS
        assert not any(task_key.startswith(prefix) for prefix in _GATE_PREFIXES)


def test_promise_keeper_maps_to_orchestrator_seat():
    # 不配模型：跟随总调度（orchestrator 席位，executor 五席已含）。
    assert agent_role_to_cluster_role("promise_keeper") == "orchestrator"
    # task_key 规则也不劫持：promise_* 不落 review/revise 泳道。
    for task_key in ("promise_contract", "promise_verify", "promise_register"):
        assert agent_role_to_cluster_role("promise_keeper", task_key) == "orchestrator"


def test_promise_keeper_handler_registered():
    from proseforge.application.agents.promise_handlers import promise_keeper_handler

    assert handler_for("promise_keeper") is promise_keeper_handler


# ---------------------------------------------------------------------------
# scene_writer 注入【本章承诺契约】块（_FakeUow 纯内存，无 DB）
# ---------------------------------------------------------------------------


class _RecordingProvider:
    provider_id = "fake"

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield GenerationEvent("response.started")
        yield GenerationEvent("content.delta", text='{"title": "t", "content": "正文内容"}')
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}})

    async def list_models(self):
        return []

    async def validate_credentials(self):
        return {"valid": True}

    async def count_tokens(self, request):
        return 1


class _FakeArtifactRow:
    def __init__(self, artifact_id: str, payload: str):
        self.id = artifact_id
        self.payload = payload


class _FakeUow:
    """纯内存 uow：session.get 按 id 返回预置 artifact 行（无 DB、无网络）。"""

    def __init__(self, row: _FakeArtifactRow | None):
        self.session = self
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, _model, ident):
        return self._row if self._row is not None and self._row.id == ident else None


def _scene_context(provider, contract_payload: dict | None) -> dict[str, object]:
    row = _FakeArtifactRow("art-contract", json.dumps(contract_payload, ensure_ascii=False)) if contract_payload is not None else None
    artifacts = [{"id": "art-contract", "artifact_type": "report", "task_key": "promise_contract", "preview": "契约卡"}] if row else []
    return {
        "run": {"id": "run-1", "goal": "写第2章《云涌》\n目标字数：不少于 10 字"},
        "task": {"id": "task-1", "role": "scene_writer", "task_key": "scene_a"},
        "provider": provider,
        "provider_id": "openai",
        "model": "gpt-4.1-mini",
        "artifacts": artifacts,
        "uow_factory": lambda: _FakeUow(row),
    }


@pytest.mark.asyncio
async def test_scene_writer_injects_promise_contract_card():
    provider = _RecordingProvider()
    payload = {
        "summary": "契约",
        "due": [{"key": "师父的遗物", "source_chapter": 1, "evidence": "木匣", "reason": "约定本章回收"}],
        "plant": [{"hook": "玉佩裂痕"}],
        "watch": [{"topic": "左臂带伤"}],
    }

    await default_role_handler(_scene_context(provider, payload))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【本章承诺契约】" in user_text
    assert "「师父的遗物」" in user_text
    assert "玉佩裂痕" in user_text
    assert "左臂带伤" in user_text


@pytest.mark.asyncio
async def test_scene_writer_without_contract_stays_quiet():
    # 无 promise_contract artifact（契约节点缺失/未跑）：不注入，不报错
    provider = _RecordingProvider()

    result = await default_role_handler(_scene_context(provider, None))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【本章承诺契约】" not in user_text
    assert result.payload["content"] == "正文内容"


@pytest.mark.asyncio
async def test_scene_writer_degraded_contract_not_injected():
    provider = _RecordingProvider()
    payload = {"summary": "降级", "due": [], "plant": [], "watch": [], "degraded": True}

    await default_role_handler(_scene_context(provider, payload))

    user_text = provider.requests[0].input_blocks[0]["text"]
    assert "【本章承诺契约】" not in user_text
