"""v1 发送路由的 reasoning_level 入队前校验（M11 修复回归）。

此前 v1 发送路由（``POST /api/v1/conversations/{id}/messages``，前端唯一使用
的发送入口）容忍透传 ``reasoning_level`` 不做校验：模型不支持的级别静默入队，
worker 端被吞成 ``{"supported": False}``，用户无感知。现与 v2 同规则：入队前
按目标模型 catalog 校验，未知级别或模型不支持的级别在路由层直接 422。
"""

from __future__ import annotations

import types
from typing import ClassVar

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from proseforge.api.routes import conversations


class _FakeConversations:
    async def branch_belongs_to_conversation(self, branch_id, conversation_id, user_id):
        return True


class _FakeModelCatalog:
    def __init__(self, entry):
        self.entry = entry

    async def get(self, provider, model):
        return self.entry


class _FakeSession:
    async def scalar(self, _statement):
        return None  # project_mode 查询：未知 → 调度按普通直答处理


class _FakeUow:
    def __init__(self, catalog_entry=None):
        self.conversations = _FakeConversations()
        self.model_catalog = _FakeModelCatalog(catalog_entry)
        self.session = _FakeSession()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


class _FakeDispatch:
    # 统一调度入口（W3）：send 路由的下游从 SendMessage 变为
    # dispatch_normal_message，本文件的补丁点随之迁移。
    calls: ClassVar[list[dict]] = []


async def _fake_dispatch_normal_message(uow_factory, queue, **kwargs):
    _FakeDispatch.calls.append(kwargs)
    return {"user_message_id": "m-user", "assistant_message_id": "m-assistant", "task_id": "task-1"}


def _catalog_with_reasoning():
    return types.SimpleNamespace(
        capabilities={"reasoning": True, "reasoning_parameter": "reasoning_effort"},
        context_window=128000,
        max_output_tokens=4096,
    )


def _patch_dependencies(monkeypatch: pytest.MonkeyPatch, catalog_entry=None):
    _FakeDispatch.calls = []
    monkeypatch.setattr(conversations, "unit_of_work", lambda request: _FakeUow(catalog_entry))
    monkeypatch.setattr("proseforge.application.dispatch.dispatch_normal_message", _fake_dispatch_normal_message)

    async def _available_model_refs(uow, user_id):
        # L11 可用性检查与本文件无关（这些用例测 reasoning 校验）；固定有可用
        # 模型，让缺省模型的请求走到 catalog 校验。
        return [("openai", "gpt-4.1-mini")]

    monkeypatch.setattr(conversations, "available_model_refs", _available_model_refs)
    state = types.SimpleNamespace(
        queue=None,
        settings=types.SimpleNamespace(master_key=SecretStr("x" * 32), environment="development"),
    )
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _payload(**overrides) -> conversations.MessageRequest:
    fields = {"branch_id": "b1", "content": "你好", "client_request_id": "cr-1"}
    fields.update(overrides)
    return conversations.MessageRequest(**fields)


@pytest.mark.asyncio
async def test_v1_send_unsupported_level_returns_422_before_enqueue(monkeypatch: pytest.MonkeyPatch):
    # 未知模型（catalog 缺失 → fallback 不支持 reasoning）配非 auto 级别 → 422，不入队。
    request = _patch_dependencies(monkeypatch, catalog_entry=None)

    with pytest.raises(HTTPException) as excinfo:
        await conversations.send_message("c1", _payload(provider="local", model="writer", reasoning_level="high"), request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    detail = excinfo.value.detail
    assert detail["code"] == "UNSUPPORTED_REASONING_LEVEL"
    assert detail["retryable"] is False
    assert detail["details"]["supported_levels"] == ["auto"]
    assert _FakeDispatch.calls == []  # 未落库、未入队


@pytest.mark.asyncio
async def test_v1_send_unknown_level_returns_422(monkeypatch: pytest.MonkeyPatch):
    request = _patch_dependencies(monkeypatch, catalog_entry=_catalog_with_reasoning())

    with pytest.raises(HTTPException) as excinfo:
        await conversations.send_message("c1", _payload(reasoning_level="bogus"), request, types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["details"]["supported_levels"] == ["auto", "none", "low", "medium", "high", "xhigh", "max"]
    assert _FakeDispatch.calls == []


@pytest.mark.asyncio
async def test_v1_send_supported_level_enqueues_normally(monkeypatch: pytest.MonkeyPatch):
    # catalog 支持 reasoning 的模型配受支持级别 → 正常落库入队，级别原样透传。
    request = _patch_dependencies(monkeypatch, catalog_entry=_catalog_with_reasoning())

    result = await conversations.send_message("c1", _payload(reasoning_level="high"), request, types.SimpleNamespace(id="u1"))

    assert _FakeDispatch.calls[0]["reasoning_level"] == "high"
    assert result == {"user_message_id": "m-user", "assistant_message_id": "m-assistant", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_v1_send_auto_passes_for_models_without_reasoning(monkeypatch: pytest.MonkeyPatch):
    # 默认路径（前端不带 reasoning_level → auto）对任何模型都放行，行为与修复前一致。
    request = _patch_dependencies(monkeypatch, catalog_entry=None)

    result = await conversations.send_message("c1", _payload(provider="local", model="writer"), request, types.SimpleNamespace(id="u1"))

    assert _FakeDispatch.calls[0]["reasoning_level"] == "auto"
    assert result["task_id"] == "task-1"
