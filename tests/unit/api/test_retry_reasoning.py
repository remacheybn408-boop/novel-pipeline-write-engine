"""retry/continue 复用消息落库的思考强度与目标模型（V2-004 fix round 2/3）。

此前 retry 入队不带 reasoning_level，worker 端 ``payload.get("reasoning_level", "auto")``
静默回落 auto——用户选的 deep/max 在重试时丢失。现：显式指定优先（入队前过
catalog 校验，不支持 → 422，与 send 同规则）；否则复用
``message.reasoning_snapshot`` 里落库的原级别；无快照才回落 auto。

provider/model 同理（fix round 3）：省略时复用 ``message.model_snapshot``，
不再回落 pydantic 默认值把非默认模型的消息重试到错误的模型上。
"""

from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from proseforge.api.routes import conversations


class _FakeConversations:
    def __init__(self):
        self.statuses: list[tuple[str, str]] = []

    async def set_message_status(self, message_id, status):
        self.statuses.append((message_id, status))


class _FakeModelCatalog:
    def __init__(self, entry):
        self.entry = entry

    async def get(self, provider, model):
        return self.entry


class _FakeUow:
    def __init__(self, catalog_entry=None):
        self.conversations = _FakeConversations()
        self.model_catalog = _FakeModelCatalog(catalog_entry)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


class _FakeQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))
        return "task-1"


def _request(queue: _FakeQueue):
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(queue=queue)))


def _catalog_with_reasoning():
    return types.SimpleNamespace(
        capabilities={"reasoning": True, "reasoning_parameter": "reasoning_effort"},
        context_window=128000,
        max_output_tokens=4096,
    )


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, snapshot: dict | None, model_snapshot: dict | None = None, catalog_entry=None) -> _FakeQueue:
    queue = _FakeQueue()
    message = types.SimpleNamespace(status="FAILED", reasoning_snapshot=snapshot, model_snapshot=model_snapshot, agent_run_id=None)

    async def _owned_message(message_id, user, request):
        return message

    monkeypatch.setattr(conversations, "_owned_message", _owned_message)
    monkeypatch.setattr(conversations, "unit_of_work", lambda request: _FakeUow(catalog_entry))
    return queue


@pytest.mark.asyncio
async def test_retry_reuses_the_messages_stored_reasoning_level(monkeypatch: pytest.MonkeyPatch):
    queue = _patch_dependencies(monkeypatch, {"level": "deep", "parameter": "reasoning_effort"})
    payload = conversations.MessageControlRequest(provider="anthropic", model="claude-sonnet")

    result = await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    task_name, enqueued = queue.enqueued[0]
    assert task_name == "proseforge.chat.generate"
    assert enqueued["reasoning_level"] == "deep"  # 不再静默降级为 auto
    assert result["status"] == "PENDING"


@pytest.mark.asyncio
async def test_retry_explicit_reasoning_level_wins_over_snapshot(monkeypatch: pytest.MonkeyPatch):
    queue = _patch_dependencies(monkeypatch, {"level": "deep"}, catalog_entry=_catalog_with_reasoning())
    payload = conversations.MessageControlRequest(provider="openai", model="gpt-4.1-mini", reasoning_level="low")

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert queue.enqueued[0][1]["reasoning_level"] == "low"


@pytest.mark.asyncio
async def test_retry_without_snapshot_falls_back_to_auto(monkeypatch: pytest.MonkeyPatch):
    queue = _patch_dependencies(monkeypatch, None)
    payload = conversations.MessageControlRequest(provider="openai", model="gpt-4.1-mini")

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert queue.enqueued[0][1]["reasoning_level"] == "auto"


@pytest.mark.asyncio
async def test_retry_reuses_level_from_unsupported_snapshot(monkeypatch: pytest.MonkeyPatch):
    # 原消息按不支持级别落库的快照也原样复用——重试复现原行为，不悄悄改级。
    queue = _patch_dependencies(monkeypatch, {"level": "max", "supported": False, "reason": "unsupported"})
    payload = conversations.MessageControlRequest(provider="local", model="writer")

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert queue.enqueued[0][1]["reasoning_level"] == "max"


@pytest.mark.asyncio
async def test_retry_without_overrides_reuses_the_model_snapshot(monkeypatch: pytest.MonkeyPatch):
    # 空载荷 {}：provider/model 复用落库 model_snapshot，不再回落默认值
    # 把非默认模型的消息重试到 openai/gpt-4.1-mini。
    queue = _patch_dependencies(
        monkeypatch,
        {"level": "deep"},
        model_snapshot={"provider": "anthropic", "model": "claude-sonnet", "context_window": 200000, "max_output_tokens": 8192, "source": "catalog"},
    )
    payload = conversations.MessageControlRequest()

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    enqueued = queue.enqueued[0][1]
    assert enqueued["provider"] == "anthropic"
    assert enqueued["model"] == "claude-sonnet"
    assert enqueued["reasoning_level"] == "deep"


@pytest.mark.asyncio
async def test_retry_without_any_snapshot_keeps_the_default_model(monkeypatch: pytest.MonkeyPatch):
    # 两个快照都缺失（历史消息）才回落默认模型与 auto，行为与旧默认一致。
    queue = _patch_dependencies(monkeypatch, None, model_snapshot=None)
    payload = conversations.MessageControlRequest()

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    enqueued = queue.enqueued[0][1]
    assert enqueued["provider"] == "openai"
    assert enqueued["model"] == "gpt-4.1-mini"
    assert enqueued["reasoning_level"] == "auto"


@pytest.mark.asyncio
async def test_retry_explicit_provider_model_still_win_over_snapshot(monkeypatch: pytest.MonkeyPatch):
    queue = _patch_dependencies(monkeypatch, None, model_snapshot={"provider": "anthropic", "model": "claude-sonnet"})
    payload = conversations.MessageControlRequest(provider="openai", model="gpt-4.1")

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    enqueued = queue.enqueued[0][1]
    assert enqueued["provider"] == "openai"
    assert enqueued["model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_retry_explicit_level_is_validated_against_the_resolved_model(monkeypatch: pytest.MonkeyPatch):
    # 省略 provider/model 时显式 level 按 snapshot 模型的 catalog 校验：支持则通过。
    queue = _patch_dependencies(monkeypatch, None, model_snapshot={"provider": "anthropic", "model": "claude-sonnet"}, catalog_entry=_catalog_with_reasoning())
    payload = conversations.MessageControlRequest(reasoning_level="high")

    await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert queue.enqueued[0][1]["reasoning_level"] == "high"


@pytest.mark.asyncio
async def test_retry_explicit_unsupported_level_returns_422_before_requeue(monkeypatch: pytest.MonkeyPatch):
    # 与 send 相同的入队前校验：未知模型（fallback 不支持 reasoning）→ 422，
    # 不翻状态、不入队。
    queue = _patch_dependencies(monkeypatch, None, catalog_entry=None)
    payload = conversations.MessageControlRequest(provider="local", model="writer", reasoning_level="high")

    with pytest.raises(HTTPException) as excinfo:
        await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert detail["code"] == "UNSUPPORTED_REASONING_LEVEL"
    assert detail["details"]["supported_levels"] == ["auto"]
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_retry_explicit_unknown_level_returns_422(monkeypatch: pytest.MonkeyPatch):
    queue = _patch_dependencies(monkeypatch, None, catalog_entry=_catalog_with_reasoning())
    payload = conversations.MessageControlRequest(reasoning_level="bogus")

    with pytest.raises(HTTPException) as excinfo:
        await conversations._requeue_message("m1", payload, _request(queue), types.SimpleNamespace(id="u1"), {"FAILED", "PARTIAL"})

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["details"]["supported_levels"] == ["auto", "none", "low", "medium", "high", "xhigh", "max"]
    assert queue.enqueued == []


def test_reasoning_level_resolution_has_a_single_canonical_home():
    # fix round 4：retry 与 regenerate 曾各持一份逐字相同的级别解析；现收敛为
    # branches.py 单一出处，conversations.py 复用（与 _validate_reasoning_level 同一模式）。
    from proseforge.api.routes import branches

    assert conversations._resolve_reasoning_level is branches._resolve_reasoning_level
    assert not hasattr(conversations, "_resolve_retry_reasoning_level")
    assert not hasattr(branches, "_resolve_regenerate_reasoning_level")


def test_target_model_resolution_has_a_single_canonical_home():
    # fix round 5：retry/continue 与 regenerate 曾各持一份逐字相同的目标模型解析
    # （仅第二参数名不同）；与级别解析同法收敛为 branches.py 单一出处，conversations.py 复用。
    from proseforge.api.routes import branches

    assert conversations._resolve_target_model is branches._resolve_target_model
    assert not hasattr(conversations, "_resolve_retry_target_model")
    assert not hasattr(branches, "_resolve_regenerate_target_model")
