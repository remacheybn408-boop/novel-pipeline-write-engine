"""resolve_rag_query: orchestrator-slot retrieval intent for scene-pack RAG.

Success path returns the model-distilled query (capped at 200 chars) plus a
rag.query_intent audit payload; any model failure / timeout / empty answer
falls back to the raw run goal. Fake provider only — no DB, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from proseforge.application.retrieval.proxy import (
    RAG_QUERY_INTENT_MAX_CHARS,
    resolve_rag_query,
)
from proseforge.domain.ports.model_provider import GenerationEvent


class _FakeProvider:
    provider_id = "fake"

    def __init__(self, text: str = "", *, fail: bool = False, hang: bool = False):
        self._text = text
        self._fail = fail
        self._hang = hang
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self._fail:
            raise RuntimeError("provider down")
        if self._hang:
            await asyncio.sleep(60)
            return
        yield GenerationEvent("content.delta", text=self._text)


GOAL = "写第三章：林雪在雨夜古堡与管家对峙，揭露遗嘱秘密。"


@pytest.mark.asyncio
async def test_intent_query_produced_by_orchestrator_model():
    provider = _FakeProvider("林雪 雨夜 古堡 对峙 遗嘱")

    query, event = await resolve_rag_query(provider, "orch-model", GOAL)

    assert query == "林雪 雨夜 古堡 对峙 遗嘱"
    assert event["source"] == "orchestrator"
    assert event["model"] == "orch-model"
    assert event["query"] == query
    request = provider.requests[0]
    assert request.model == "orch-model"
    assert request.input_blocks[0]["text"] == GOAL


@pytest.mark.asyncio
async def test_intent_query_capped_at_max_chars():
    provider = _FakeProvider("长" * 500)

    query, event = await resolve_rag_query(provider, "m", GOAL)

    assert len(query) == RAG_QUERY_INTENT_MAX_CHARS
    assert event["source"] == "orchestrator"


@pytest.mark.asyncio
async def test_model_failure_falls_back_to_goal():
    query, event = await resolve_rag_query(_FakeProvider(fail=True), "m", GOAL)

    assert query == GOAL
    assert event["source"] == "goal_fallback"
    assert event["reason"] == "model-error"


@pytest.mark.asyncio
async def test_timeout_falls_back_to_goal():
    query, event = await resolve_rag_query(_FakeProvider(hang=True), "m", GOAL, timeout=0.05)

    assert query == GOAL
    assert event["source"] == "goal_fallback"
    assert event["reason"] == "model-error"


@pytest.mark.asyncio
async def test_no_provider_falls_back_to_goal():
    query, event = await resolve_rag_query(None, "m", GOAL)

    assert query == GOAL
    assert event["reason"] == "provider-unavailable"


@pytest.mark.asyncio
async def test_empty_goal_skips_model_call():
    provider = _FakeProvider("irrelevant")

    query, event = await resolve_rag_query(provider, "m", "   ")

    assert query == "   "
    assert event["reason"] == "empty-goal"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_empty_model_answer_falls_back_to_goal():
    query, event = await resolve_rag_query(_FakeProvider("  \n "), "m", GOAL)

    assert query == GOAL
    assert event["reason"] == "empty-intent"
