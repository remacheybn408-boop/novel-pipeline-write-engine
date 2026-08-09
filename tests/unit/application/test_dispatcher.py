"""Normal-mode dispatch routing (unified dispatcher skeleton).

chat intent (or a non-work project) -> SendMessage direct reply, the exact
pre-dispatcher behavior; write/review/revise/analyze on a work project ->
the orchestrator model re-judges the rule hit first (question-shaped
messages match writing keywords too): an explicit chat verdict replies
inline, another work verdict creates the run from that intent, and any
failure (None) keeps the rule result. Runs go through run_entry_response
with force_single_model=True so every cluster lane collapses onto the
user's selected model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from proseforge.application.dispatch import dispatcher

_BASE = {
    "master_key": SecretStr("x" * 32),
    "environment": "development",
    "branch_id": "b-1",
    "client_request_id": "req-1",
    "user_id": "u-1",
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "reasoning_level": "auto",
}


def _patch_send_message(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    class _FakeSendMessage:
        def __init__(self, uow_factory, queue) -> None:
            pass

        async def execute(self, **kwargs):
            calls.append(kwargs)
            return (
                SimpleNamespace(id="user-msg-1"),
                SimpleNamespace(id="assistant-msg-1"),
                "chat-task-1",
            )

    monkeypatch.setattr(dispatcher, "SendMessage", _FakeSendMessage)
    return calls


def _patch_run_entry(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def _fake_run_entry_response(uow_factory, queue, **kwargs):
        calls.append(kwargs)
        return {"user_message_id": "user-msg-1", "assistant_message_id": "assistant-msg-1", "task_id": "run-1", "intent": kwargs["intent"], "agent_run_id": "run-1"}

    monkeypatch.setattr(dispatcher, "run_entry_response", _fake_run_entry_response)
    return calls


def _patch_orchestrator(monkeypatch: pytest.MonkeyPatch, verdict: str | None) -> list[dict]:
    """Stub the orchestrator second pass; verdict None simulates failure."""
    calls: list[dict] = []

    async def _fake_classify(uow_factory, **kwargs):
        calls.append(kwargs)
        return verdict

    monkeypatch.setattr(dispatcher, "_classify_with_orchestrator", _fake_classify)
    return calls


@pytest.mark.asyncio
async def test_chat_intent_goes_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    orchestrator_calls = _patch_orchestrator(monkeypatch, "write")

    result = await dispatcher.dispatch_normal_message(
        None, None, content="你好，今天怎么样？", project_mode="work", **_BASE
    )

    assert result == {"user_message_id": "user-msg-1", "assistant_message_id": "assistant-msg-1", "task_id": "chat-task-1"}
    assert len(send_calls) == 1
    assert run_calls == []
    # Rule-chat messages never pay the orchestrator second pass.
    assert orchestrator_calls == []


@pytest.mark.asyncio
async def test_work_project_write_intent_collapses_to_single_model_run(monkeypatch: pytest.MonkeyPatch) -> None:
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    orchestrator_calls = _patch_orchestrator(monkeypatch, "write")

    result = await dispatcher.dispatch_normal_message(
        None, None, content="帮我写第三章", project_mode="work", **_BASE
    )

    assert send_calls == []
    assert len(run_calls) == 1
    assert run_calls[0]["intent"] == "write"
    assert run_calls[0]["force_single_model"] is True
    assert run_calls[0]["provider"] == "openai"
    assert result["agent_run_id"] == "run-1"
    # The second pass ran on the user's selected model in every slot
    # (normal-mode collapse semantics).
    assert len(orchestrator_calls) == 1
    roles = orchestrator_calls[0]["roles"]
    assert roles.orchestrator == ("openai", "gpt-4.1-mini")
    assert roles.write == ("openai", "gpt-4.1-mini")


@pytest.mark.asyncio
async def test_orchestrator_chat_verdict_overrides_rule_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    # "帮我看看这个大纲怎么样" matches the write keyword 大纲 but is a
    # question: the orchestrator's explicit chat verdict replies inline
    # instead of triggering a collapsed run.
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    _patch_orchestrator(monkeypatch, "chat")

    result = await dispatcher.dispatch_normal_message(
        None, None, content="帮我看看这个大纲怎么样", project_mode="work", **_BASE
    )

    assert len(send_calls) == 1
    assert run_calls == []
    assert result["task_id"] == "chat-task-1"


@pytest.mark.asyncio
async def test_orchestrator_failure_keeps_rule_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # Timeout/any failure surfaces as None: availability first, the rule
    # classification stands.
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    _patch_orchestrator(monkeypatch, None)

    result = await dispatcher.dispatch_normal_message(
        None, None, content="帮我写第三章", project_mode="work", **_BASE
    )

    assert send_calls == []
    assert len(run_calls) == 1
    assert run_calls[0]["intent"] == "write"
    assert result["agent_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_orchestrator_other_work_verdict_redirects_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rule says write, orchestrator says review: the run is created from
    # the orchestrator's verdict.
    _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    _patch_orchestrator(monkeypatch, "review")

    await dispatcher.dispatch_normal_message(
        None, None, content="帮我写第三章", project_mode="work", **_BASE
    )

    assert len(run_calls) == 1
    assert run_calls[0]["intent"] == "review"


@pytest.mark.asyncio
async def test_chat_project_stays_direct_even_for_write_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)
    orchestrator_calls = _patch_orchestrator(monkeypatch, "write")
    classify_calls: list[str] = []
    original = dispatcher.classify_intent

    def _spy(text: str):
        classify_calls.append(text)
        return original(text)

    monkeypatch.setattr(dispatcher, "classify_intent", _spy)

    result = await dispatcher.dispatch_normal_message(
        None, None, content="帮我写第三章", project_mode="chat", **_BASE
    )

    # Chat projects never reroute — and neither classifier is invoked.
    assert classify_calls == []
    assert orchestrator_calls == []
    assert len(send_calls) == 1
    assert run_calls == []
    assert result["task_id"] == "chat-task-1"


@pytest.mark.asyncio
async def test_unknown_project_mode_stays_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    send_calls = _patch_send_message(monkeypatch)
    run_calls = _patch_run_entry(monkeypatch)

    await dispatcher.dispatch_normal_message(
        None, None, content="帮我写第三章", project_mode=None, **_BASE
    )

    assert len(send_calls) == 1
    assert run_calls == []
