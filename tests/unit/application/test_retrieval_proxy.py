"""retrieve_for_context: the unified RAG proxy (W4).

Covers the two contract points: distillation failure falls back to the raw
query, and project_id/owner/snapshot linkage are passed through to
NarrativeRetriever.build unchanged. Fake provider + fake uow only — the
retriever build is stubbed, no DB, no network.
"""

from __future__ import annotations

import pytest

from proseforge.application.retrieval.proxy import retrieve_for_context
from proseforge.application.work.retriever import ScenePack
from proseforge.domain.ports.model_provider import GenerationEvent

GOAL = "写第三章：林雪在雨夜古堡与管家对峙，揭露遗嘱秘密。"


class _FakeProvider:
    provider_id = "fake"

    def __init__(self, text: str = "", *, fail: bool = False):
        self._text = text
        self._fail = fail
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self._fail:
            raise RuntimeError("provider down")
        yield GenerationEvent("content.delta", text=self._text)


class _FakeSession:
    def __init__(self, owner_id: str | None):
        self._owner_id = owner_id

    async def scalar(self, _statement):
        return self._owner_id


class _FakeUow:
    session_factory = None  # build is stubbed; the factory is never used

    def __init__(self, owner_id: str | None = "owner-1"):
        self.session = _FakeSession(owner_id)


def _patch_build(monkeypatch, calls: list[dict]) -> None:
    async def fake_build(self, **kwargs):
        calls.append(kwargs)
        return ScenePack(text="pack-text", sections={"worldview": "w"}, evidence=[], run_id="rr", token_cost=1)

    monkeypatch.setattr("proseforge.application.work.retriever.NarrativeRetriever.build", fake_build)


@pytest.mark.asyncio
async def test_distilled_query_reaches_retriever(monkeypatch):
    calls: list[dict] = []
    _patch_build(monkeypatch, calls)
    provider = _FakeProvider("林雪 雨夜 古堡 对峙 遗嘱")

    pack = await retrieve_for_context(
        _FakeUow(), project_id="proj-1", query=GOAL, orchestrator_ref=(provider, "orch-model")
    )

    assert pack is not None and pack.text == "pack-text"
    assert calls[0]["query"] == "林雪 雨夜 古堡 对峙 遗嘱"


@pytest.mark.asyncio
async def test_distill_failure_falls_back_to_raw_query(monkeypatch):
    calls: list[dict] = []
    _patch_build(monkeypatch, calls)
    events: list[dict] = []

    async def on_query(_distilled: str, intent_event: dict[str, object]) -> None:
        events.append(intent_event)

    pack = await retrieve_for_context(
        _FakeUow(), project_id="proj-1", query=GOAL,
        orchestrator_ref=(_FakeProvider(fail=True), "m"), on_query=on_query,
    )

    assert pack is not None
    assert calls[0]["query"] == GOAL  # raw query, not the failed intent
    assert events == [{"source": "goal_fallback", "reason": "model-error"}]


@pytest.mark.asyncio
async def test_no_orchestrator_slot_uses_raw_query(monkeypatch):
    calls: list[dict] = []
    _patch_build(monkeypatch, calls)

    pack = await retrieve_for_context(_FakeUow(), project_id="proj-1", query=GOAL)

    assert pack is not None
    assert calls[0]["query"] == GOAL


@pytest.mark.asyncio
async def test_project_id_owner_and_snapshot_linkage_passed_through(monkeypatch):
    calls: list[dict] = []
    _patch_build(monkeypatch, calls)

    await retrieve_for_context(
        _FakeUow(owner_id="owner-9"), project_id="proj-7", query=GOAL,
        conversation_id="conv-3", message_id="msg-4",
    )

    assert calls[0]["project_id"] == "proj-7"
    assert calls[0]["user_id"] == "owner-9"  # server-resolved project owner
    assert calls[0]["conversation_id"] == "conv-3"
    assert calls[0]["message_id"] == "msg-4"


@pytest.mark.asyncio
async def test_unknown_project_returns_none_without_build(monkeypatch):
    calls: list[dict] = []
    _patch_build(monkeypatch, calls)

    pack = await retrieve_for_context(_FakeUow(owner_id=None), project_id="missing", query=GOAL)

    assert pack is None
    assert calls == []
