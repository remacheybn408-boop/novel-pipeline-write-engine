"""Message attachments: attachment_ids on POST /conversations/{id}/messages.

Validation (ownership + same project, else 400), message_id backfill on the
persisted user message, list_messages serialization, and swarm run-goal
injection (`[附件: 文件名]` prefix block on the run goal).
Real app on native sqlite (TestClient + lifespan), same fixture style as
test_swarm_messages.py.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.agents import AgentRunModel
from proseforge.infrastructure.database.models.remaining import AttachmentModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Same L11 guard bypass as test_swarm_messages: these cases do not test
    # the no-available-models 422, so pin one available model.
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


def _conversation(client: TestClient, slug: str, mode: str = "chat") -> tuple[str, str, str]:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Proj", "mode": mode})
    assert response.status_code == 201
    project_id = response.json()["id"]
    response = client.post("/api/v1/conversations", json={"project_id": project_id, "title": "聊天"})
    assert response.status_code == 200
    return project_id, response.json()["id"], response.json()["branch_id"]


def _upload(client: TestClient, project_id: str, filename: str = "notes.txt", body: bytes = b"attachment body text") -> str:
    response = client.post(f"/api/v1/projects/{project_id}/files", files={"file": (filename, body, "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _send(client: TestClient, conversation_id: str, branch_id: str, content: str, **extra):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"branch_id": branch_id, "content": content, "client_request_id": f"cr-{content}", **extra},
    )


def _attachment_row(client: TestClient, attachment_id: str):
    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            row = await uow.session.get(AttachmentModel, attachment_id)
            return None if row is None else (row.message_id, row.project_id, row.filename)

    return asyncio.run(_read())


def test_send_with_attachment_links_and_serializes(client: TestClient):
    project_id, conversation_id, branch_id = _conversation(client, "att-chat")
    attachment_id = _upload(client, project_id)
    assert _attachment_row(client, attachment_id)[0] is None  # unlinked before send

    response = _send(client, conversation_id, branch_id, "帮我看看这个文件", attachment_ids=[attachment_id])

    assert response.status_code == 200, response.text
    user_message_id = response.json()["user_message_id"]
    # Backfill happened in the same transaction as the message persist.
    assert _attachment_row(client, attachment_id)[0] == user_message_id
    messages = client.get(f"/api/v1/conversations/{conversation_id}/branches/{branch_id}/messages").json()
    user_message = next(item for item in messages if item["id"] == user_message_id)
    assert user_message["attachments"] == [{"id": attachment_id, "filename": "notes.txt"}]
    # The persisted content stays clean; injection happens at generation time.
    assert user_message["content"] == "帮我看看这个文件"


def test_unknown_attachment_id_is_400(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client, "att-unknown")

    response = _send(client, conversation_id, branch_id, "你好", attachment_ids=["does-not-exist"])

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid attachment id"


def test_attachment_from_other_project_is_400(client: TestClient):
    _project_id, conversation_id, branch_id = _conversation(client, "att-main")
    other_project_id, _other_conversation, _other_branch = _conversation(client, "att-other")
    foreign_attachment = _upload(client, other_project_id)

    response = _send(client, conversation_id, branch_id, "你好", attachment_ids=[foreign_attachment])

    assert response.status_code == 400
    # The rejected send must not link the attachment anywhere.
    assert _attachment_row(client, foreign_attachment)[0] is None


def test_swarm_run_goal_includes_attachment_text(client: TestClient):
    project_id, conversation_id, branch_id = _conversation(client, "att-swarm", mode="work")
    attachment_id = _upload(client, project_id, filename="outline.txt", body="第三章大纲：雨夜重逢".encode())

    response = _send(client, conversation_id, branch_id, "写第三章", mode="swarm", attachment_ids=[attachment_id])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "write" and body["agent_run_id"]

    async def _read():
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            run = await uow.session.get(AgentRunModel, body["agent_run_id"])
            attachment = await uow.session.get(AttachmentModel, attachment_id)
            return run.goal, attachment.message_id

    goal, linked_message_id = asyncio.run(_read())
    assert linked_message_id == body["user_message_id"]
    assert goal.startswith("[附件: outline.txt]\n第三章大纲：雨夜重逢")
    assert goal.endswith("写第三章")


def test_swarm_chat_intent_keeps_attachment_for_worker_injection(client: TestClient):
    # chat intent does not build a run goal; the worker injects from the
    # message link instead (covered by inject_history_attachments tests).
    project_id, conversation_id, branch_id = _conversation(client, "att-idle", mode="work")
    attachment_id = _upload(client, project_id)

    body = _send(client, conversation_id, branch_id, "你好", mode="swarm", attachment_ids=[attachment_id]).json()

    assert body["intent"] == "chat" and body["agent_run_id"] is None
    assert _attachment_row(client, attachment_id)[0] == body["user_message_id"]
