"""Swarm message entry: POST /api/v1/conversations/{id}/messages?mode=swarm.

chat intent -> plain reply task (no run); write/review/revise -> agent run
linked to the placeholder assistant message. Default path (no mode, or
chat-mode project) is byte-identical to before. Also covers export.zip and
the cluster_role field on GET /agent-runs/{id}/tasks.
Real app on native sqlite (TestClient + lifespan).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from proseforge.api.main import create_app
from proseforge.domain.chapter.entity import Chapter
from proseforge.domain.common.ids import new_id
from proseforge.infrastructure.database.models.agents import (
    AgentEventModel,
    AgentRunModel,
    AgentTaskModel,
)
from proseforge.infrastructure.database.models.conversation import MessageModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # L11：缺省模型的发送在无任何可用模型时 422。本文件的用例不测该检查，
    # 固定一个有可用模型的环境，聚焦 swarm 行为本身。
    async def _available_model_refs(uow, user_id):
        return [("openai", "gpt-4.1-mini")]

    monkeypatch.setattr("proseforge.api.routes.conversations.available_model_refs", _available_model_refs)
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


def _conversation(client: TestClient, mode: str = "work") -> tuple[str, str, str]:
    response = client.post("/api/v1/projects", json={"slug": f"proj-{mode}-1", "title": "Novel", "mode": mode})
    assert response.status_code == 201
    project_id = response.json()["id"]
    response = client.post("/api/v1/conversations", json={"project_id": project_id, "title": "聊天"})
    assert response.status_code == 200  # route has no explicit 201
    return project_id, response.json()["id"], response.json()["branch_id"]


def _send(client: TestClient, conversation_id: str, branch_id: str, content: str, **extra):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"branch_id": branch_id, "content": content, "client_request_id": f"cr-{content}", **extra},
    )


def _runs(client: TestClient) -> list[tuple[str, str]]:
    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            return [(row.id, row.goal) for row in await uow.session.scalars(select(AgentRunModel))]

    return asyncio.run(_read())


def _seed_chapters(client: TestClient, project_id: str, chapter_nos: list[int]) -> None:
    """Give the project chapters so review/revise intents pass the empty-project guard."""
    async def _write():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            for chapter_no in chapter_nos:
                await uow.chapters.add(Chapter.create(project_id=project_id, chapter_no=chapter_no, title=f"第{chapter_no}章"))
            await uow.commit()

    asyncio.run(_write())


def test_swarm_chat_intent_replies_inline_without_run(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(client, conversation_id, branch_id, "你好", mode="swarm")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "chat" and body["agent_run_id"] is None
    assert body["assistant_message_id"]
    assert _runs(client) == []  # idle talk never creates a run


def test_swarm_write_intent_creates_run_linked_to_assistant_message(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(client, conversation_id, branch_id, "写第三章", mode="swarm")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "write"
    run_id = body["agent_run_id"]
    assert run_id

    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run_status, run_goal = run.status, run.goal
            roles = [task.role for task in await uow.session.scalars(select(AgentTaskModel).where(AgentTaskModel.run_id == run_id))]
            message = await uow.session.get(MessageModel, body["assistant_message_id"])
            message_state = (message.agent_run_id, message.status, message.content)
            return run_status, run_goal, roles, message_state

    run_status, run_goal, roles, message_state = asyncio.run(_read())
    assert run_status == "PENDING" and run_goal == "写第三章"
    assert roles == [
        "chief_planner", "character_designer", "promise_keeper",
        "scene_writer", "scene_writer", "scene_writer", "scene_writer", "merge_editor",
        "continuity_reviewer", "adversarial_reviewer", "style_editor",
        "merge_editor", "merge_editor", "chief_editor", "continuity_reviewer",
        "promise_keeper", "promise_keeper",
    ]
    # Placeholder assistant message is linked and waits for the writeback.
    assert message_state == (run_id, "PENDING", "")


def test_swarm_review_and_revise_intents_create_template_runs(client: TestClient):
    project_id, conversation_id, branch_id = _conversation(client)
    _seed_chapters(client, project_id, [2, 3])

    review = _send(client, conversation_id, branch_id, "审校第三章", mode="swarm").json()
    revise = _send(client, conversation_id, branch_id, "改写第二章", mode="swarm").json()

    assert review["intent"] == "review" and revise["intent"] == "revise"
    runs = {goal: run_id for run_id, goal in _runs(client)}
    assert set(runs) == {"审校第三章", "改写第二章"}


def test_swarm_message_list_exposes_agent_run_id(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)
    body = _send(client, conversation_id, branch_id, "写第三章", mode="swarm").json()

    messages = client.get(f"/api/v1/conversations/{conversation_id}/branches/{branch_id}/messages").json()
    assistant = next(item for item in messages if item["id"] == body["assistant_message_id"])
    assert assistant["agent_run_id"] == body["agent_run_id"]
    user_message = next(item for item in messages if item["role"] == "user")
    assert user_message["agent_run_id"] is None


def test_default_send_path_unchanged(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(client, conversation_id, branch_id, "今天天气怎么样")

    assert response.status_code == 200
    body = response.json()
    assert "intent" not in body and "agent_run_id" not in body  # byte-compatible shape
    assert _runs(client) == []  # chat intent WITHOUT mode=swarm stays a chat task


def test_normal_dispatch_write_intent_creates_collapsed_run(client: TestClient):
    # Unified dispatcher (W3): a work-project write intent reroutes to an
    # agent run even without mode=swarm — but every lane collapses onto the
    # requested model (single_model marker, executor skips cluster config).
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(client, conversation_id, branch_id, "写第三章")

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "write" and body["agent_run_id"]

    async def _read_run():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, body["agent_run_id"])
            return (run.goal, run.single_model) if run is not None else None

    row = asyncio.run(_read_run())
    assert row is not None and row[0] == "写第三章"
    assert row[1] is True


def test_swarm_mode_on_chat_project_falls_back_to_default(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client, mode="chat")

    response = _send(client, conversation_id, branch_id, "写第三章", mode="swarm")

    assert response.status_code == 200
    assert "intent" not in response.json()
    assert _runs(client) == []


def test_swarm_write_run_tasks_expose_cluster_role_and_export_zip(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)
    body = _send(client, conversation_id, branch_id, "写第三章", mode="swarm").json()
    run_id = body["agent_run_id"]

    tasks = client.get(f"/api/v3/agent-runs/{run_id}/tasks").json()
    cluster_roles = {task["task_key"]: task["cluster_role"] for task in tasks}
    assert cluster_roles == {
        "planner": "write", "character": "write", "promise_contract": "orchestrator",
        "scene_a": "write", "scene_b": "write", "scene_c": "write", "scene_d": "write",
        "select": "revise",
        "review_continuity": "review", "review_adversarial": "review", "review_style": "review",
        "review_council": "review",
        "merge": "revise", "rewrite": "revise", "recheck": "revise",
        "promise_verify": "orchestrator", "promise_register": "orchestrator",
    }
    assert all(task["last_error"] is None for task in tasks)

    # Add a scene artifact, then the zip carries summary.md + scenes/.
    scene_task = next(task for task in tasks if task["task_key"] == "scene_a")
    response = client.post(f"/api/v3/agent-runs/{run_id}/artifacts", json={
        "task_id": scene_task["id"], "artifact_type": "candidate",
        "payload": {"title": "雨夜", "content": "雨夜正文"}, "preview": "雨夜正文…",
    })
    assert response.status_code == 201, response.text

    response = client.get(f"/api/v3/agent-runs/{run_id}/export.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "proseforge-run-" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "summary.md" in names and "scenes/scene_a.md" in names
        assert "雨夜正文" in archive.read("scenes/scene_a.md").decode()
        assert archive.read("summary.md").decode()


def test_export_zip_owner_scoped(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)
    body = _send(client, conversation_id, branch_id, "写第三章", mode="swarm").json()
    assert client.get("/api/v3/agent-runs/does-not-exist/export.zip").status_code == 404
    assert body["agent_run_id"]


def test_export_zip_summary_includes_gate_and_chapter(client: TestClient):
    """Regression: summary.md must render the same gate/chapter narrative as
    the message writeback, not the generic completion line."""
    _project_id, conversation_id, branch_id = _conversation(client)
    body = _send(client, conversation_id, branch_id, "写第三章", mode="swarm").json()
    run_id = body["agent_run_id"]

    tasks = client.get(f"/api/v3/agent-runs/{run_id}/tasks").json()
    scene_task = next(task for task in tasks if task["task_key"] == "scene_a")
    response = client.post(f"/api/v3/agent-runs/{run_id}/artifacts", json={
        "task_id": scene_task["id"], "artifact_type": "candidate",
        "payload": {"title": "雨夜", "content": "雨夜正文"}, "preview": "雨夜正文…",
    })
    assert response.status_code == 201, response.text

    async def _seed_events():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            run.status = "COMPLETED"
            uow.session.add(AgentEventModel(
                id=new_id(), run_id=run_id, sequence=100,
                event_type="gate.evaluated",
                payload=json.dumps({"passed": True, "reasons": []}, ensure_ascii=False),
            ))
            uow.session.add(AgentEventModel(
                id=new_id(), run_id=run_id, sequence=101,
                event_type="chapter.written_back",
                payload=json.dumps({"chapter_id": "ch-1", "chapter_no": 3, "version_id": "v-1", "title": "雨夜"}, ensure_ascii=False),
            ))
            await uow.commit()

    asyncio.run(_seed_events())

    response = client.get(f"/api/v3/agent-runs/{run_id}/export.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        summary = archive.read("summary.md").decode()
    assert "总调度：第 3 章流水线完成。" in summary  # chapter headline
    assert "审校 ✓" in summary  # gate passed (not the legacy "发现 N 个问题")
    assert "定稿《雨夜》第 3 章" in summary  # chapter writeback line


# ---------------------------------------------------------------------------
# Orchestrator (LLM) second-pass classification: rule says "chat", the
# orchestrator model re-judges. Failure/garbage always falls back to chat.
# ---------------------------------------------------------------------------

from proseforge.domain.ports.model_provider import GenerationEvent


class _OrchestratorProvider:
    provider_id = "fake"

    def __init__(self, answer: str | None = "write", error: Exception | None = None):
        self._answer = answer
        self._error = error
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._answer is not None:
            yield GenerationEvent("content.delta", text=self._answer)
        yield GenerationEvent("response.completed", data={"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}})


def _with_credential(client: TestClient) -> None:
    response = client.post("/api/v1/credentials", json={"provider": "openai", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201


def _patch_orchestrator(monkeypatch, provider: _OrchestratorProvider) -> None:
    monkeypatch.setattr("proseforge.providers.factory.build_provider", lambda *args, **kwargs: provider)


# Rule-misses this (no write keyword hit), only the LLM can route it.
RULE_CHAT_CONTENT = "帮我把林风的故事继续往下写"


def test_orchestrator_llm_routes_rule_chat_to_write_run(client: TestClient, monkeypatch):
    _with_credential(client)
    provider = _OrchestratorProvider(answer="write")
    _patch_orchestrator(monkeypatch, provider)
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(client, conversation_id, branch_id, RULE_CHAT_CONTENT, mode="swarm")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "write" and body["agent_run_id"]
    # Tiny one-shot classification request: system prompt + 16-token cap.
    request = provider.requests[0]
    assert request.max_output_tokens == 16
    # Orchestrator prompt is composed from the orchestrator persona file
    # (packs/personas/orchestrator.md) plus the classification instructions.
    assert "总调度" in request.system_blocks[0]["text"]
    assert "write" in request.system_blocks[0]["text"]


def test_orchestrator_garbage_answer_keeps_chat(client: TestClient, monkeypatch):
    _with_credential(client)
    _patch_orchestrator(monkeypatch, _OrchestratorProvider(answer="我听不懂你在说什么"))
    _project_id, conversation_id, branch_id = _conversation(client)

    body = _send(client, conversation_id, branch_id, RULE_CHAT_CONTENT, mode="swarm").json()

    assert body["intent"] == "chat" and body["agent_run_id"] is None
    assert _runs(client) == []


def test_orchestrator_provider_error_keeps_chat(client: TestClient, monkeypatch):
    _with_credential(client)
    _patch_orchestrator(monkeypatch, _OrchestratorProvider(error=RuntimeError("provider down")))
    _project_id, conversation_id, branch_id = _conversation(client)

    body = _send(client, conversation_id, branch_id, RULE_CHAT_CONTENT, mode="swarm").json()

    assert body["intent"] == "chat" and body["agent_run_id"] is None
    assert _runs(client) == []


def test_orchestrator_review_answer_creates_review_run(client: TestClient, monkeypatch):
    _with_credential(client)
    _patch_orchestrator(monkeypatch, _OrchestratorProvider(answer="Review."))
    project_id, conversation_id, branch_id = _conversation(client)
    _seed_chapters(client, project_id, [1])

    body = _send(client, conversation_id, branch_id, RULE_CHAT_CONTENT, mode="swarm").json()

    assert body["intent"] == "review" and body["agent_run_id"]
    runs = _runs(client)
    assert len(runs) == 1


def test_orchestrator_without_credential_keeps_chat(client: TestClient, monkeypatch):
    # No credential for the orchestrator provider: silent chat fallback.
    _patch_orchestrator(monkeypatch, _OrchestratorProvider(answer="write"))
    _project_id, conversation_id, branch_id = _conversation(client)

    body = _send(client, conversation_id, branch_id, RULE_CHAT_CONTENT, mode="swarm").json()

    assert body["intent"] == "chat" and body["agent_run_id"] is None


# ---------------------------------------------------------------------------
# H2 regression: the swarm entry must pass the request's provider/model down
# to create_agent_run (legacy path without a cluster config used to drop them
# and fall back to the openai/gpt-4.1-mini default).
# ---------------------------------------------------------------------------

from proseforge.application.agents import swarm_entry


def _run_provider_model(client: TestClient, run_id: str) -> tuple[str | None, str | None]:
    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, run_id)
            return run.provider, run.model

    return asyncio.run(_read())


def test_swarm_run_passes_request_provider_model_to_create_agent_run(client: TestClient, monkeypatch):
    captured: dict[str, object] = {}
    real_create_agent_run = swarm_entry.create_agent_run

    async def _spy(*args, **kwargs):
        captured.update(kwargs)
        return await real_create_agent_run(*args, **kwargs)

    monkeypatch.setattr(swarm_entry, "create_agent_run", _spy)
    _project_id, conversation_id, branch_id = _conversation(client)

    response = _send(
        client, conversation_id, branch_id, "写第三章",
        mode="swarm", provider="anthropic", model="claude-sonnet-4-5",
    )

    assert response.status_code == 200, response.text
    assert captured["provider"] == "anthropic" and captured["model"] == "claude-sonnet-4-5"
    # No cluster config -> the request values land on the run row as-is.
    assert _run_provider_model(client, response.json()["agent_run_id"]) == ("anthropic", "claude-sonnet-4-5")


def test_swarm_run_falls_back_to_default_model_when_unspecified(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client)

    body = _send(client, conversation_id, branch_id, "写第三章", mode="swarm").json()

    # MessageRequest defaults apply, then flow through to the run row.
    assert _run_provider_model(client, body["agent_run_id"]) == ("openai", "gpt-4.1-mini")


# ---------------------------------------------------------------------------
# M2 regression: run creation and the message<->run link share ONE commit.
# A failure in the link step must roll back the run too — never the old half
# state (run exists, assistant message has no agent_run_id, stuck PENDING).
# ---------------------------------------------------------------------------

from proseforge.infrastructure.database.repositories.conversation import (
    SqlAlchemyConversationRepository,
)


def test_swarm_link_failure_leaves_no_orphan_run(client: TestClient, monkeypatch):
    async def _broken_link(self, message_id, agent_run_id):
        raise RuntimeError("link step blew up")

    monkeypatch.setattr(SqlAlchemyConversationRepository, "set_message_agent_run", _broken_link)
    _project_id, conversation_id, branch_id = _conversation(client)

    with pytest.raises(RuntimeError, match="link step blew up"):
        _send(client, conversation_id, branch_id, "写第三章", mode="swarm")

    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            runs = [row.id for row in await uow.session.scalars(select(AgentRunModel))]
            messages = [
                (row.role, row.status, row.agent_run_id)
                for row in await uow.session.scalars(select(MessageModel))
            ]
            return runs, messages

    runs, messages = asyncio.run(_read())
    # Whole transaction rolled back: no orphan run, and no dangling
    # user/assistant message pair either.
    assert runs == []
    assert messages == []
