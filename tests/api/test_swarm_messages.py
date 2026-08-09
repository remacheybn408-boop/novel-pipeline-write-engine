"""PG-only (B1 batch): swarm message entry end-to-end with migration 0042.

sqlite cannot prove the messages.agent_run_id column survives the real
migration chain — conftest upgrades alembic head (0042) before these run.
Covers: swarm chat intent creates NO run; swarm write intent creates the
run + placeholder assistant message with the link visible in the message
list; export.zip 200.
"""

from __future__ import annotations

import uuid

import pytest

ORIGIN = "http://testserver"  # aligned with tests/api/conftest.py


@pytest.fixture()
def swarm_user(client, api_settings, user_headers_factory):
    user_id = f"swarm-{uuid.uuid4().hex[:12]}"
    headers = user_headers_factory(user_id)
    return client, headers, user_id


def _conversation(api, headers):
    response = api.post("/api/v1/projects", json={"slug": f"swarm-{uuid.uuid4().hex[:10]}", "title": "Swarm", "mode": "work"}, headers=headers)
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    response = api.post("/api/v1/conversations", json={"project_id": project_id, "title": "聊天"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"], response.json()["branch_id"]


def _send(api, headers, conversation_id, branch_id, content, **extra):
    return api.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"branch_id": branch_id, "content": content, "client_request_id": f"cr-{uuid.uuid4().hex[:8]}", **extra},
        headers=headers,
    )


def test_swarm_chat_intent_creates_no_run(client, api_settings, swarm_user):
    api, headers, _ = swarm_user
    conversation_id, branch_id = _conversation(api, headers)

    # Explicit provider/model: skips the no-available-models 422 guard (L11);
    # these tests cover run creation/linking, not model resolution.
    response = _send(api, headers, conversation_id, branch_id, "你好", mode="swarm", provider="openai", model="gpt-4.1-mini")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "chat" and body["agent_run_id"] is None
    messages = api.get(f"/api/v1/conversations/{conversation_id}/branches/{branch_id}/messages", headers=headers).json()
    assert all(item["agent_run_id"] is None for item in messages)


def test_swarm_write_intent_creates_linked_run_and_export_zip(client, api_settings, swarm_user):
    api, headers, _ = swarm_user
    conversation_id, branch_id = _conversation(api, headers)

    response = _send(api, headers, conversation_id, branch_id, "写第三章", mode="swarm", provider="openai", model="gpt-4.1-mini")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "write" and body["agent_run_id"]
    # The placeholder assistant message carries the run link (migration 0042).
    messages = api.get(f"/api/v1/conversations/{conversation_id}/branches/{branch_id}/messages", headers=headers).json()
    assistant = next(item for item in messages if item["id"] == body["assistant_message_id"])
    assert assistant["agent_run_id"] == body["agent_run_id"] and assistant["status"] == "PENDING"
    # Run shape: intent graph template persisted on real PG.
    run = api.get(f"/api/v3/agent-runs/{body['agent_run_id']}", headers=headers).json()
    assert run["status"] == "PENDING"
    tasks = api.get(f"/api/v3/agent-runs/{body['agent_run_id']}/tasks", headers=headers).json()
    # Write intent runs the full 12-task pipeline (plan -> characters ->
    # 3 parallel scene drafts -> select winner -> 3 parallel reviews ->
    # merge -> rewrite -> recheck).
    assert [task["cluster_role"] for task in tasks] == ["write"] * 5 + ["revise"] + ["review"] * 3 + ["revise"] * 3
    # export.zip is a 200 even before any artifact exists (summary.md only).
    response = api.get(f"/api/v3/agent-runs/{body['agent_run_id']}/export.zip", headers=headers)
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
