"""Offline orchestrator loop test with a stubbed uow module, fake provider
continuation and a fake registered tool. Heavy dependencies (sqlalchemy) are
stubbed in sys.modules before the lazy import inside run_tool_rounds fires.
"""

from __future__ import annotations

import json
import sys
import types
from typing import ClassVar

import pytest
from pydantic import BaseModel

from proseforge.application.tools import orchestrator
from proseforge.application.tools.registry import TOOL_REGISTRY, ToolDef
from proseforge.application.tools.types import ToolResult


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.status = "COMPLETED"


class _FakeConversations:
    def __init__(self, message):
        self.message = message

    async def get_message(self, message_id):
        return self.message

    async def set_message_content(self, message_id, content):
        self.message.content = content

    async def set_content_hash(self, message_id, digest):
        self.hash = digest

    async def set_message_status(self, message_id, status):
        self.message.status = status

    async def conversation_id_for_message(self, message_id):
        return "conv-1"


class _FakeStates:
    def __init__(self, toggles):
        self._rows = [types.SimpleNamespace(skill_key=key, enabled=value) for key, value in toggles.items()]

    async def list_for_user(self, user_id):
        return self._rows


class _FakeToolCalls:
    def __init__(self):
        self.rows = {}

    async def get(self, call_id):
        return self.rows.get(call_id)

    async def create(self, **fields):
        record = types.SimpleNamespace(**fields)
        self.rows[fields["call_id"]] = record
        return record


class _FakeUow:
    message = None
    toggles: ClassVar[dict] = {}
    tool_calls = None

    def __init__(self):
        self.conversations = _FakeConversations(_FakeUow.message)
        self.builtin_skill_states = _FakeStates(_FakeUow.toggles)
        self.tool_calls = _FakeUow.tool_calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def commit(self):
        return None


class _FakeStream:
    def __init__(self):
        self.payloads = []

    async def publish(self, channel, payload):
        self.payloads.append((channel, payload))


class _FakeGenerateReply:
    calls = 0

    def __init__(self, uow_factory, provider, event_stream):
        pass

    async def execute(self, **kwargs):
        _FakeGenerateReply.calls += 1
        _FakeUow.message.content += "\n（续写完成）"
        _FakeUow.message.status = "COMPLETED"
        return 1


class _EchoArgs(BaseModel):
    query: str


async def _echo_handler(args, ctx):
    return ToolResult(f"结果：{args.query}", {"echo": args.query})


FAKE_TOOL = ToolDef(
    name="echo_tool",
    schema=_EchoArgs,
    handler=_echo_handler,
    timeout_s=5.0,
    toggle_key="builtin-web-search",
    label="回声工具",
    contract_doc="## echo_tool",
)


@pytest.mark.asyncio
async def test_orchestrator_full_round(monkeypatch):
    fake_uow_module = types.ModuleType("proseforge.infrastructure.database.uow")
    fake_uow_module.SqlAlchemyUnitOfWork = lambda session_factory: _FakeUow()
    monkeypatch.setitem(sys.modules, "proseforge.infrastructure.database.uow", fake_uow_module)
    monkeypatch.setattr(orchestrator, "GenerateReply", _FakeGenerateReply)
    monkeypatch.setitem(TOOL_REGISTRY, "echo_tool", FAKE_TOOL)

    _FakeUow.message = _FakeMessage('回答前\n```tool: {"name": "echo_tool", "args": {"query": "你好"}}\n```\n回答后')
    _FakeUow.toggles = {"builtin-web-search": True}
    _FakeUow.tool_calls = _FakeToolCalls()
    _FakeGenerateReply.calls = 0
    stream = _FakeStream()
    settings = types.SimpleNamespace(max_tool_rounds=4, tool_result_max_chars=8000)

    rounds = await orchestrator.run_tool_rounds(
        session_factory=None, event_stream=stream, provider=None,
        message_id="m1", user_id="u1", provider_id="p", model="m",
        system_blocks=[], base_input_blocks=[], max_output_tokens=8192,
        reasoning=None, settings=settings,
    )

    assert rounds == 1
    assert _FakeGenerateReply.calls == 1  # continuation happened
    content = _FakeUow.message.content
    assert "<!-- tool:done:" in content and "结果：你好" in content and "```tool:" not in content
    assert content.endswith("（续写完成）")
    # one log row, status done
    assert len(_FakeUow.tool_calls.rows) == 1
    row = next(iter(_FakeUow.tool_calls.rows.values()))
    assert row.status == "done" and row.tool_name == "echo_tool" and row.result_summary == "结果：你好"
    assert json.loads(row.params_json) == {"query": "你好"}
    # SSE started + done on the message channel; done also fans out to conversation
    statuses = [p for ch, p in stream.payloads if p.get("event") == "message.tool.status" and ch == "message:m1"]
    assert [s["status"] for s in statuses] == ["started", "done"]
    assert statuses[0]["tool"] == "echo_tool" and statuses[1]["call_id"] == row.call_id
    channels = {ch for ch, p in stream.payloads if p.get("event") == "message.tool.status"}
    assert "message:m1" in channels and "conversation:conv-1" in channels


@pytest.mark.asyncio
async def test_orchestrator_unknown_tool_writes_validation_block(monkeypatch):
    fake_uow_module = types.ModuleType("proseforge.infrastructure.database.uow")
    fake_uow_module.SqlAlchemyUnitOfWork = lambda session_factory: _FakeUow()
    monkeypatch.setitem(sys.modules, "proseforge.infrastructure.database.uow", fake_uow_module)
    monkeypatch.setattr(orchestrator, "GenerateReply", _FakeGenerateReply)

    _FakeUow.message = _FakeMessage('```tool: {"name": "nope", "args": {}}\n```')
    _FakeUow.toggles = {}
    _FakeUow.tool_calls = _FakeToolCalls()
    stream = _FakeStream()
    settings = types.SimpleNamespace(max_tool_rounds=4, tool_result_max_chars=8000)

    rounds = await orchestrator.run_tool_rounds(
        session_factory=None, event_stream=stream, provider=None,
        message_id="m1", user_id="u1", provider_id="p", model="m",
        system_blocks=[], base_input_blocks=[], max_output_tokens=8192,
        reasoning=None, settings=settings,
    )
    assert rounds == 1  # error block written + continuation ran
    assert "未知工具" in _FakeUow.message.content
    row = next(iter(_FakeUow.tool_calls.rows.values()))
    assert row.status == "failed" and row.error_class == "validation"
    statuses = [p for _, p in stream.payloads if p.get("event") == "message.tool.status"]
    assert statuses[-1]["status"] == "failed" and statuses[-1]["error_class"] == "validation"


@pytest.mark.asyncio
async def test_orchestrator_cache_reuse_skips_execution(monkeypatch):
    fake_uow_module = types.ModuleType("proseforge.infrastructure.database.uow")
    fake_uow_module.SqlAlchemyUnitOfWork = lambda session_factory: _FakeUow()
    monkeypatch.setitem(sys.modules, "proseforge.infrastructure.database.uow", fake_uow_module)
    monkeypatch.setattr(orchestrator, "GenerateReply", _FakeGenerateReply)
    monkeypatch.setitem(TOOL_REGISTRY, "echo_tool", FAKE_TOOL)

    _FakeUow.message = _FakeMessage('```tool: {"name": "echo_tool", "args": {"query": "你好"}}\n```')
    _FakeUow.toggles = {"builtin-web-search": True}
    _FakeUow.tool_calls = _FakeToolCalls()
    # Seed a done row with the deterministic call_id: retry must reuse it.
    from proseforge.application.tools.orchestrator import tool_call_id

    call_id = tool_call_id("m1", "echo_tool", {"query": "你好"})
    _FakeUow.tool_calls.rows[call_id] = types.SimpleNamespace(status="done", result_summary="结果：你好")
    handler_calls = []

    async def counting_handler(args, ctx):
        handler_calls.append(1)
        return ToolResult("should not run")

    monkeypatch.setitem(TOOL_REGISTRY, "echo_tool", ToolDef("echo_tool", _EchoArgs, counting_handler, 5.0, "builtin-web-search", "回声工具", ""))
    stream = _FakeStream()
    settings = types.SimpleNamespace(max_tool_rounds=4, tool_result_max_chars=8000)

    rounds = await orchestrator.run_tool_rounds(
        session_factory=None, event_stream=stream, provider=None,
        message_id="m1", user_id="u1", provider_id="p", model="m",
        system_blocks=[], base_input_blocks=[], max_output_tokens=8192,
        reasoning=None, settings=settings,
    )
    assert rounds == 1 and not handler_calls  # reused, not re-executed
    assert "结果：你好" in _FakeUow.message.content
    # no second row with the same call_id PK: reuse skips the insert
    assert len(_FakeUow.tool_calls.rows) == 1
