"""regenerate 复用源消息落库的思考强度与目标模型（V2-004 fix round 3/4）。

此前 ``RegenerateRequest`` 没有 ``reasoning_level``，路由不读源消息
``reasoning_snapshot["level"]``，``RegenerateReply`` 入队也不带该键，worker 端
``payload.get("reasoning_level", "auto")`` 静默回落 auto——用户选的 deep/max
在 regenerate 时丢失。现与 retry 同规则：显式指定优先（入队前过 catalog
校验，不支持 → 422）；缺省复用落库级别（含现已不支持的级别也原样复用，
不悄悄改级）；无快照才回落 auto。

provider/model 同理（fix round 4）：省略时复用源消息 ``model_snapshot``，
不再回落 pydantic 默认值把非默认模型的消息 regenerate 到
openai/gpt-4.1-mini；显式级别的 422 校验同样按解析后的目标模型查 catalog。
"""

from __future__ import annotations

import types
from typing import ClassVar

import pytest
from fastapi import HTTPException

from proseforge.api.routes import branches


class _FakeConversations:
    def __init__(self, source, conversation_id: str = "c1"):
        self.source = source
        self.conversation_id = conversation_id

    async def get_message(self, message_id):
        return self.source

    async def conversation_id_for_message(self, message_id):
        return self.conversation_id if self.source is not None else None

    async def belongs_to_owner(self, conversation_id, user_id):
        return True


class _FakeModelCatalog:
    def __init__(self, entry):
        self.entry = entry

    async def get(self, provider, model):
        return self.entry


class _FakeUow:
    def __init__(self, source, catalog_entry=None, conversation_id: str = "c1"):
        self.conversations = _FakeConversations(source, conversation_id)
        self.model_catalog = _FakeModelCatalog(catalog_entry)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


class _FakeRegenerateReply:
    calls: ClassVar[list[dict]] = []

    def __init__(self, uow_factory, queue):
        pass

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(id="m-new"), "task-1"


def _catalog_with_reasoning():
    return types.SimpleNamespace(
        capabilities={"reasoning": True, "reasoning_parameter": "reasoning_effort"},
        context_window=128000,
        max_output_tokens=4096,
    )


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, snapshot: dict | None, model_snapshot: dict | None = None, catalog_entry=None, message_conversation_id: str = "c1", source_role: str = "assistant"):
    _FakeRegenerateReply.calls = []
    source = types.SimpleNamespace(branch_id="b1", parent_message_id="u1", reasoning_snapshot=snapshot, model_snapshot=model_snapshot, role=source_role)
    monkeypatch.setattr(branches, "unit_of_work", lambda request: _FakeUow(source, catalog_entry, message_conversation_id))
    monkeypatch.setattr(branches, "RegenerateReply", _FakeRegenerateReply)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(queue=None)))


@pytest.mark.asyncio
async def test_regenerate_explicit_reasoning_level_is_honored(monkeypatch: pytest.MonkeyPatch):
    request = _patch_dependencies(monkeypatch, {"level": "low"}, catalog_entry=_catalog_with_reasoning())
    payload = branches.RegenerateRequest(provider="openai", model="gpt-4.1", reasoning_level="high")

    result = await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert _FakeRegenerateReply.calls[0]["reasoning_level"] == "high"
    assert _FakeRegenerateReply.calls[0]["provider"] == "openai"
    assert _FakeRegenerateReply.calls[0]["model"] == "gpt-4.1"
    assert result == {"message_id": "m-new", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_regenerate_reuses_the_source_messages_stored_level(monkeypatch: pytest.MonkeyPatch):
    request = _patch_dependencies(monkeypatch, {"level": "deep", "parameter": "reasoning_effort"})
    payload = branches.RegenerateRequest(provider="openai", model="gpt-4.1")

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert _FakeRegenerateReply.calls[0]["reasoning_level"] == "deep"  # 不再静默降级为 auto


@pytest.mark.asyncio
async def test_regenerate_without_overrides_reuses_the_model_snapshot(monkeypatch: pytest.MonkeyPatch):
    # 空载荷 {}（UI 默认路径）：provider/model 复用源消息落库 model_snapshot，
    # 不再回落 pydantic 默认值把非默认模型的消息 regenerate 到 openai/gpt-4.1-mini。
    request = _patch_dependencies(
        monkeypatch,
        {"level": "deep"},
        model_snapshot={"provider": "anthropic", "model": "claude-sonnet", "context_window": 200000, "max_output_tokens": 8192, "source": "catalog"},
    )
    payload = branches.RegenerateRequest()

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    call = _FakeRegenerateReply.calls[0]
    assert call["provider"] == "anthropic"
    assert call["model"] == "claude-sonnet"
    assert call["reasoning_level"] == "deep"


@pytest.mark.asyncio
async def test_regenerate_without_any_snapshot_keeps_the_default_model(monkeypatch: pytest.MonkeyPatch):
    # 两个快照都缺失（历史消息）才回落默认模型与 auto，行为与旧默认一致。
    request = _patch_dependencies(monkeypatch, None, model_snapshot=None)
    payload = branches.RegenerateRequest()

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    call = _FakeRegenerateReply.calls[0]
    assert call["provider"] == "openai"
    assert call["model"] == "gpt-4.1-mini"
    assert call["reasoning_level"] == "auto"


@pytest.mark.asyncio
async def test_regenerate_explicit_provider_model_still_win_over_snapshot(monkeypatch: pytest.MonkeyPatch):
    request = _patch_dependencies(monkeypatch, None, model_snapshot={"provider": "anthropic", "model": "claude-sonnet"})
    payload = branches.RegenerateRequest(provider="openai", model="gpt-4.1")

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    call = _FakeRegenerateReply.calls[0]
    assert call["provider"] == "openai"
    assert call["model"] == "gpt-4.1"


@pytest.mark.asyncio
async def test_regenerate_reuses_unsupported_stored_level_verbatim(monkeypatch: pytest.MonkeyPatch):
    # 落库快照里的级别现已不被 catalog 支持（未知模型 → fallback）也原样复用——
    # 复用路径不做 422、不悄悄改级，与 retry 的钉住行为一致。
    request = _patch_dependencies(monkeypatch, {"level": "max", "supported": False, "reason": "unsupported"}, catalog_entry=None)
    payload = branches.RegenerateRequest(provider="local", model="writer")

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert _FakeRegenerateReply.calls[0]["reasoning_level"] == "max"


@pytest.mark.asyncio
async def test_regenerate_explicit_level_is_validated_against_the_resolved_model(monkeypatch: pytest.MonkeyPatch):
    # 省略 provider/model 时显式 level 按 snapshot 模型的 catalog 校验：支持则通过，
    # 且入队的也是解析后的目标模型（而非 pydantic 默认值）。
    request = _patch_dependencies(monkeypatch, None, model_snapshot={"provider": "anthropic", "model": "claude-sonnet"}, catalog_entry=_catalog_with_reasoning())
    payload = branches.RegenerateRequest(reasoning_level="high")

    await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    call = _FakeRegenerateReply.calls[0]
    assert call["provider"] == "anthropic"
    assert call["model"] == "claude-sonnet"
    assert call["reasoning_level"] == "high"


@pytest.mark.asyncio
async def test_regenerate_explicit_unsupported_level_returns_422(monkeypatch: pytest.MonkeyPatch):
    # 显式指定与 send 同规则：未知模型（fallback 不支持 reasoning）→ 入队前 422。
    request = _patch_dependencies(monkeypatch, {"level": "auto"}, catalog_entry=None)
    payload = branches.RegenerateRequest(provider="local", model="writer", reasoning_level="high")

    with pytest.raises(HTTPException) as excinfo:
        await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert detail["code"] == "UNSUPPORTED_REASONING_LEVEL"
    assert detail["details"]["supported_levels"] == ["auto"]
    assert _FakeRegenerateReply.calls == []  # 未创建候选、未入队


@pytest.mark.asyncio
async def test_regenerate_explicit_unknown_level_returns_422(monkeypatch: pytest.MonkeyPatch):
    request = _patch_dependencies(monkeypatch, None, catalog_entry=_catalog_with_reasoning())
    payload = branches.RegenerateRequest(reasoning_level="bogus")

    with pytest.raises(HTTPException) as excinfo:
        await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["details"]["supported_levels"] == ["auto", "none", "low", "medium", "high", "xhigh", "max"]
    assert _FakeRegenerateReply.calls == []


@pytest.mark.asyncio
async def test_regenerate_rejects_message_from_another_conversation(monkeypatch: pytest.MonkeyPatch):
    # Cross-account regression: owning the URL conversation is not enough —
    # a message_id from a different conversation must 404 instead of enqueueing.
    request = _patch_dependencies(monkeypatch, {"level": "deep"}, message_conversation_id="c-other")
    payload = branches.RegenerateRequest()

    with pytest.raises(HTTPException) as excinfo:
        await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 404
    assert _FakeRegenerateReply.calls == []  # 未创建候选、未入队


@pytest.mark.asyncio
async def test_regenerate_rejects_user_message_with_422(monkeypatch: pytest.MonkeyPatch):
    # 对 user 消息调 regenerate 没有模型输出可重做：入队前 422，
    # 只有 assistant 消息能重新生成。
    request = _patch_dependencies(monkeypatch, {"level": "auto"}, source_role="user")
    payload = branches.RegenerateRequest()

    with pytest.raises(HTTPException) as excinfo:
        await branches.regenerate_reply("c1", "m1", payload, request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    assert _FakeRegenerateReply.calls == []  # 未创建候选、未入队
