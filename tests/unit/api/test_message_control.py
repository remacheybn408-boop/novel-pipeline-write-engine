"""消息控制端点的三条审计修复（L8/L9/L11）回归测试。

- L8：retry/continue 不得作用于 swarm 占位消息（带 agent_run_id），否则会把
  集群结果降级成普通单模型回复 → 409，引导去 run 详情页。
- L9：stop 与 worker 完成之间的 TOCTOU——状态预检与写入之间 worker 可能已
  COMPLETED；改为条件 UPDATE，竞态落败（影响行数 0）→ 409，不覆盖终态。
- L11：未指定模型且系统无任何可用模型时，send 曾静默回落 openai/gpt-4.1-mini
  入队一个注定失败的任务；现路由层 422 并指引去设置页配置凭证。显式指定
  模型的请求行为不变。
"""

from __future__ import annotations

import types
from typing import ClassVar

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from proseforge.api.routes import conversations


class _FakeConversations:
    def __init__(self, *, cancel_result: bool = True):
        self.cancel_result = cancel_result
        self.cancelled_with: list[tuple[str, set[str]]] = []
        self.statuses: list[tuple[str, str]] = []

    async def branch_belongs_to_conversation(self, branch_id, conversation_id, user_id):
        return True

    async def cancel_message_if_active(self, message_id, cancellable):
        self.cancelled_with.append((message_id, cancellable))
        return self.cancel_result

    async def conversation_id_for_message(self, message_id):
        return "conv-1"

    async def set_message_status(self, message_id, status):
        self.statuses.append((message_id, status))


class _FakeModelCatalog:
    async def get(self, provider, model):
        return None  # 未知模型 → FALLBACK_CAPABILITIES，auto 级别通过校验


class _FakeSession:
    async def scalar(self, _statement):
        return None  # project_mode 查询：未知 → 调度按普通直答处理


class _FakeUow:
    def __init__(self, conversations: _FakeConversations):
        self.conversations = conversations
        self.model_catalog = _FakeModelCatalog()
        self.session = _FakeSession()
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.committed = True


class _FakeQueue:
    def __init__(self):
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))
        return "task-1"


class _FakeEventStream:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


def _request(queue: _FakeQueue | None = None, event_stream: _FakeEventStream | None = None):
    state = types.SimpleNamespace(
        queue=queue or _FakeQueue(),
        settings=types.SimpleNamespace(master_key=SecretStr("x" * 32), environment="development"),
    )
    if event_stream is not None:
        state.event_stream = event_stream
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def _patch_uow(monkeypatch: pytest.MonkeyPatch, uow: _FakeUow) -> None:
    monkeypatch.setattr(conversations, "unit_of_work", lambda request: uow)


# --- L8：retry/continue 拒绝 swarm 占位消息 --------------------------------


def _patch_owned_message(monkeypatch: pytest.MonkeyPatch, message) -> None:
    async def _owned_message(message_id, user, request):
        return message

    monkeypatch.setattr(conversations, "_owned_message", _owned_message)


@pytest.mark.asyncio
async def test_retry_rejects_agent_run_owned_message(monkeypatch: pytest.MonkeyPatch):
    uow = _FakeUow(_FakeConversations())
    _patch_uow(monkeypatch, uow)
    _patch_owned_message(monkeypatch, types.SimpleNamespace(status="FAILED", agent_run_id="run-1", model_snapshot=None, reasoning_snapshot=None))
    queue = _FakeQueue()

    with pytest.raises(HTTPException) as excinfo:
        await conversations.retry_message("m1", conversations.MessageControlRequest(), _request(queue), types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 409
    assert "agent run" in excinfo.value.detail
    assert queue.enqueued == []
    assert uow.conversations.statuses == []


@pytest.mark.asyncio
async def test_continue_rejects_agent_run_owned_message(monkeypatch: pytest.MonkeyPatch):
    uow = _FakeUow(_FakeConversations())
    _patch_uow(monkeypatch, uow)
    _patch_owned_message(monkeypatch, types.SimpleNamespace(status="PARTIAL", agent_run_id="run-1", model_snapshot=None, reasoning_snapshot=None))
    queue = _FakeQueue()

    with pytest.raises(HTTPException) as excinfo:
        await conversations.continue_message("m1", conversations.MessageControlRequest(), _request(queue), types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 409
    assert queue.enqueued == []


# --- L9：stop 条件取消，竞态落败 → 409 -------------------------------------


@pytest.mark.asyncio
async def test_stop_rejects_agent_run_owned_message(monkeypatch: pytest.MonkeyPatch):
    # 与 L8 的 retry/continue 同一防护：swarm 占位消息的 stop 只翻消息状态，
    # 停不掉正在执行的集群任务 → 409，引导去 run 控制接口取消 run。
    uow = _FakeUow(_FakeConversations())
    _patch_uow(monkeypatch, uow)
    _patch_owned_message(monkeypatch, types.SimpleNamespace(status="STREAMING", agent_run_id="run-1"))
    stream = _FakeEventStream()

    with pytest.raises(HTTPException) as excinfo:
        await conversations.stop_message("m1", _request(event_stream=stream), types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 409
    assert "agent run" in excinfo.value.detail
    assert uow.conversations.cancelled_with == []
    assert stream.published == []


@pytest.mark.asyncio
async def test_stop_flips_status_and_publishes_terminal_event(monkeypatch: pytest.MonkeyPatch):
    uow = _FakeUow(_FakeConversations(cancel_result=True))
    _patch_uow(monkeypatch, uow)
    _patch_owned_message(monkeypatch, types.SimpleNamespace(status="STREAMING", agent_run_id=None))
    stream = _FakeEventStream()

    result = await conversations.stop_message("m1", _request(event_stream=stream), types.SimpleNamespace(id="u1"))

    assert result == {"id": "m1", "status": "CANCELLED"}
    assert uow.conversations.cancelled_with == [("m1", {"PENDING", "STREAMING", "PARTIAL"})]
    assert uow.committed
    channels = {channel for channel, _ in stream.published}
    assert channels == {"message:m1", "conversation:conv-1"}


@pytest.mark.asyncio
async def test_stop_returns_409_when_the_worker_won_the_race(monkeypatch: pytest.MonkeyPatch):
    # 预检时还是 STREAMING，条件 UPDATE 影响 0 行 = worker 已 COMPLETED；
    # 不得覆盖终态、不得发布取消事件。
    uow = _FakeUow(_FakeConversations(cancel_result=False))
    _patch_uow(monkeypatch, uow)
    _patch_owned_message(monkeypatch, types.SimpleNamespace(status="STREAMING", agent_run_id=None))
    stream = _FakeEventStream()

    with pytest.raises(HTTPException) as excinfo:
        await conversations.stop_message("m1", _request(event_stream=stream), types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 409
    assert uow.conversations.cancelled_with == [("m1", {"PENDING", "STREAMING", "PARTIAL"})]
    assert not uow.committed
    assert stream.published == []


# --- L11：无可用模型时 send 路由层 422 --------------------------------------


class _FakeDispatch:
    # 统一调度入口（W3）：send 路由的下游从 SendMessage 变为
    # dispatch_normal_message，本文件的补丁点随之迁移。
    calls: ClassVar[list[dict]] = []


async def _fake_dispatch_normal_message(uow_factory, queue, **kwargs):
    _FakeDispatch.calls.append(kwargs)
    return {"user_message_id": "user-m1", "assistant_message_id": "assistant-m1", "task_id": "task-1"}


def _patch_send_dependencies(monkeypatch: pytest.MonkeyPatch, available: list[tuple[str, str]]) -> _FakeUow:
    uow = _FakeUow(_FakeConversations())
    _patch_uow(monkeypatch, uow)
    _FakeDispatch.calls = []
    monkeypatch.setattr("proseforge.application.dispatch.dispatch_normal_message", _fake_dispatch_normal_message)

    async def _available_model_refs(uow_arg, user_id):
        return available

    monkeypatch.setattr(conversations, "available_model_refs", _available_model_refs)
    return uow


def _send_payload(**overrides) -> conversations.MessageRequest:
    base = {"branch_id": "b1", "content": "hello", "client_request_id": "req-1"}
    return conversations.MessageRequest(**{**base, **overrides})


@pytest.mark.asyncio
async def test_send_without_model_and_no_available_model_returns_422(monkeypatch: pytest.MonkeyPatch):
    _patch_send_dependencies(monkeypatch, available=[])

    with pytest.raises(HTTPException) as excinfo:
        await conversations.send_message("conv-1", _send_payload(), _request(), types.SimpleNamespace(id="u1"))

    assert excinfo.value.status_code == 422
    assert "设置页" in excinfo.value.detail
    assert _FakeDispatch.calls == []


@pytest.mark.asyncio
async def test_send_without_model_falls_back_to_default_when_models_available(monkeypatch: pytest.MonkeyPatch):
    _patch_send_dependencies(monkeypatch, available=[("anthropic", "claude-sonnet")])

    result = await conversations.send_message("conv-1", _send_payload(), _request(), types.SimpleNamespace(id="u1"))

    assert result["assistant_message_id"] == "assistant-m1"
    assert _FakeDispatch.calls[0]["provider"] == "openai"
    assert _FakeDispatch.calls[0]["model"] == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_send_with_explicit_model_skips_the_availability_check(monkeypatch: pytest.MonkeyPatch):
    # 显式指定模型：即使可用池为空也不做 422 检查，行为与修复前一致。
    _patch_send_dependencies(monkeypatch, available=[])

    result = await conversations.send_message(
        "conv-1", _send_payload(provider="local", model="writer"), _request(), types.SimpleNamespace(id="u1")
    )

    assert result["assistant_message_id"] == "assistant-m1"
    assert _FakeDispatch.calls[0]["provider"] == "local"
    assert _FakeDispatch.calls[0]["model"] == "writer"


# --- 真 sqlite 端到端：条件 UPDATE 的 rowcount 语义与 L8 拦截 --------------
# 与 tests/unit/api/test_swarm_messages.py 同一模式（TestClient + lifespan）。

import asyncio
import base64

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        yield test_client


def _conversation(client: TestClient, mode: str = "chat") -> tuple[str, str]:
    response = client.post("/api/v1/projects", json={"slug": f"proj-{mode}-l89", "title": "Novel", "mode": mode})
    assert response.status_code == 201
    response = client.post("/api/v1/conversations", json={"project_id": response.json()["id"], "title": "聊天"})
    assert response.status_code == 200
    return response.json()["id"], response.json()["branch_id"]


def _send(client: TestClient, conversation_id: str, branch_id: str, content: str, **extra) -> dict:
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        # 显式模型：绕开 L11 可用性检查（测试库无凭证），聚焦消息控制行为。
        json={"branch_id": branch_id, "content": content, "client_request_id": f"cr-{content}", "provider": "local", "model": "writer", **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _message_status(client: TestClient, message_id: str) -> str:
    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            return await uow.conversations.message_status(message_id)

    return asyncio.run(_read())


def test_stop_completed_message_returns_409_and_keeps_terminal_status(client: TestClient):
    # L9 竞态落败路径的真实 rowcount 语义：条件 UPDATE 命中 0 行 → 409，
    # COMPLETED 终态不被覆盖。
    conversation_id, branch_id = _conversation(client)
    message_id = _send(client, conversation_id, branch_id, "你好")["assistant_message_id"]

    async def _complete():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            await uow.conversations.set_message_status(message_id, "COMPLETED")
            await uow.commit()

    asyncio.run(_complete())

    response = client.post(f"/api/v1/messages/{message_id}/stop")
    assert response.status_code == 409
    assert _message_status(client, message_id) == "COMPLETED"


def test_stop_pending_message_cancels_and_persists(client: TestClient):
    conversation_id, branch_id = _conversation(client)
    message_id = _send(client, conversation_id, branch_id, "你好")["assistant_message_id"]

    response = client.post(f"/api/v1/messages/{message_id}/stop")
    assert response.status_code == 200, response.text
    assert response.json() == {"id": message_id, "status": "CANCELLED"}
    assert _message_status(client, message_id) == "CANCELLED"


def test_retry_on_swarm_placeholder_message_returns_409(client: TestClient):
    # L8：swarm 占位消息（带 agent_run_id）的 retry 必须被拦在路由层。
    conversation_id, branch_id = _conversation(client, mode="work")
    message_id = _send(client, conversation_id, branch_id, "写第三章", mode="swarm")["assistant_message_id"]

    response = client.post(f"/api/v1/messages/{message_id}/retry", json={})
    assert response.status_code == 409
    assert "agent run" in response.json()["detail"]
    assert _message_status(client, message_id) == "PENDING"  # 未被翻状态


def test_stop_on_swarm_placeholder_message_returns_409(client: TestClient):
    # 与 L8 同一防护：swarm 占位消息的 stop 停不掉正在跑的集群任务，必须
    # 409 引导去 run 控制接口，消息状态保持不变。
    conversation_id, branch_id = _conversation(client, mode="work")
    message_id = _send(client, conversation_id, branch_id, "写第三章", mode="swarm")["assistant_message_id"]

    response = client.post(f"/api/v1/messages/{message_id}/stop")
    assert response.status_code == 409
    assert "agent run" in response.json()["detail"]
    assert _message_status(client, message_id) == "PENDING"  # 未被翻状态
