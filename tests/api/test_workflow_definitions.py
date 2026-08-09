"""V2-008 workflow definition, run-control, and SSE contracts.

These tests intentionally exercise only public HTTP surfaces.  The B4 Podman
batch supplies PostgreSQL and the authenticated client fixture.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.workflow_v2 import WorkflowNodeStateModel

VALID_DEFINITION = {
    "nodes": [
        {"id": "intake", "kind": "intake", "title": "Intake", "summary": "Read outline"},
        {"id": "draft", "kind": "write", "title": "Draft", "summary": "Write chapter"},
        {"id": "review", "kind": "review", "title": "Review", "summary": "Check quality"},
    ],
    "edges": [
        {"source": "intake", "target": "draft"},
        {"source": "draft", "target": "review"},
    ],
}


def _create_project(auth_client, *, slug: str | None = None) -> str:
    unique = slug or f"wf-{uuid4().hex[:12]}"
    response = auth_client.post_json(
        "/api/v1/projects",
        {"slug": unique, "title": "Workflow Studio"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _create_definition(auth_client, project_id: str, *, definition=None) -> dict[str, object]:
    response = auth_client.post_json(
        f"/api/v2/projects/{project_id}/workflow-definitions",
        {"name": f"chapter-flow-{uuid4().hex[:8]}", "definition": definition or VALID_DEFINITION},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _start_run(auth_client, definition_id: str, **payload) -> dict[str, object]:
    response = auth_client.post_json(
        f"/api/v2/workflow-definitions/{definition_id}/runs",
        {"token_limit": 100_000, "cost_limit": 100, **payload},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _control(auth_client, run_id: str, action: str, key: str):
    return auth_client.raw.post(
        f"/api/v2/workflow-runs/{run_id}/{action}",
        headers={"Origin": "http://testserver", "Idempotency-Key": key},
    )


def _other_user_headers(user_headers_factory) -> dict[str, str]:
    return user_headers_factory(f"other-{uuid4().hex}", role="USER")


def test_workflow_v2_endpoints_require_authentication():
    client = TestClient(create_app())

    assert client.get("/api/v2/projects/p1/workflow-definitions").status_code == 401
    assert client.post(
        "/api/v2/projects/p1/workflow-definitions",
        json={"name": "flow", "definition": VALID_DEFINITION},
    ).status_code == 401
    assert client.get("/api/v2/workflow-runs/run-1").status_code == 401
    assert client.post("/api/v2/workflow-runs/run-1/pause").status_code == 401
    assert client.get("/api/v2/workflow-runs/run-1/events").status_code == 401


def test_workflow_definition_crud_creates_immutable_revisions(auth_client):
    project_id = _create_project(auth_client)
    created = _create_definition(auth_client, project_id)
    assert created["revision"] == 1
    assert created["definition"] == VALID_DEFINITION

    listed = auth_client.get(f"/api/v2/projects/{project_id}/workflow-definitions")
    assert listed.status_code == 200
    assert any(item["id"] == created["id"] for item in listed.json())

    fetched = auth_client.get(f"/api/v2/workflow-definitions/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created

    revised_graph = {
        **VALID_DEFINITION,
        "nodes": [
            *VALID_DEFINITION["nodes"],
            {"id": "export", "kind": "export", "title": "Export", "summary": "Package manuscript"},
        ],
        "edges": [*VALID_DEFINITION["edges"], {"source": "review", "target": "export"}],
    }
    updated = auth_client.raw.put(
        f"/api/v2/workflow-definitions/{created['id']}",
        json={"definition": revised_graph},
        headers={"Origin": "http://testserver"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["definition"] == revised_graph

    deleted = auth_client.raw.delete(
        f"/api/v2/workflow-definitions/{created['id']}",
        headers={"Origin": "http://testserver"},
    )
    assert deleted.status_code == 204
    assert auth_client.get(f"/api/v2/workflow-definitions/{created['id']}").status_code == 404


@pytest.mark.parametrize(
    "definition",
    [
        {
            "nodes": [
                {"id": "a", "kind": "intake", "title": "A"},
                {"id": "b", "kind": "write", "title": "B"},
            ],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        },
        {"nodes": [{"id": "a", "kind": "teleport", "title": "A"}], "edges": []},
        {
            "nodes": [{"id": "a", "kind": "intake", "title": "A"}],
            "edges": [{"source": "a", "target": "ghost"}],
        },
        {
            "nodes": [{"id": "a", "kind": "intake", "title": "A"}],
            "edges": [{"source": "a", "target": "a"}],
        },
        {
            "nodes": [
                {"id": "same", "kind": "intake", "title": "A"},
                {"id": "same", "kind": "write", "title": "B"},
            ],
            "edges": [],
        },
    ],
    ids=["cycle", "unknown-kind", "dangling-edge", "self-loop", "duplicate-node"],
)
def test_workflow_definition_rejects_invalid_graphs(auth_client, definition):
    project_id = _create_project(auth_client)
    response = auth_client.post_json(
        f"/api/v2/projects/{project_id}/workflow-definitions",
        {"name": "invalid-flow", "definition": definition},
    )
    assert response.status_code == 422


def test_workflow_definition_name_conflict_and_ownership_are_enforced(auth_client, user_headers_factory):
    project_id = _create_project(auth_client)
    name = f"owned-flow-{uuid4().hex[:8]}"
    first = auth_client.post_json(
        f"/api/v2/projects/{project_id}/workflow-definitions",
        {"name": name, "definition": VALID_DEFINITION},
    )
    assert first.status_code == 201
    duplicate = auth_client.post_json(
        f"/api/v2/projects/{project_id}/workflow-definitions",
        {"name": name, "definition": VALID_DEFINITION},
    )
    assert duplicate.status_code == 409

    definition_id = first.json()["id"]
    headers = _other_user_headers(user_headers_factory)
    assert auth_client.raw.get(f"/api/v2/workflow-definitions/{definition_id}", headers=headers).status_code == 404
    assert auth_client.raw.put(
        f"/api/v2/workflow-definitions/{definition_id}",
        json={"definition": VALID_DEFINITION},
        headers=headers,
    ).status_code == 404
    assert auth_client.raw.delete(
        f"/api/v2/workflow-definitions/{definition_id}",
        headers=headers,
    ).status_code == 404


def test_start_run_pins_definition_revision_and_returns_refresh_snapshot(auth_client, user_headers_factory):
    project_id = _create_project(auth_client)
    definition = _create_definition(auth_client, project_id)
    started = _start_run(auth_client, str(definition["id"]))

    run = started["run"]
    nodes = started["nodes"]
    assert run["definition_id"] == definition["id"]
    assert run["definition_revision"] == definition["revision"]
    assert run["status"] == "RUNNING"
    assert {node["node_key"] for node in nodes} == {"intake", "draft", "review"}
    assert all(node["status"] == "PENDING" for node in nodes)

    snapshot = auth_client.get(f"/api/v2/workflow-runs/{run['id']}")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["run"]["id"] == run["id"]
    assert body["run"]["definition_revision"] == 1
    assert body["event_cursor"] >= 1
    assert {node["node_key"] for node in body["nodes"]} == {"intake", "draft", "review"}

    headers = _other_user_headers(user_headers_factory)
    assert auth_client.raw.get(f"/api/v2/workflow-runs/{run['id']}", headers=headers).status_code == 404


def test_control_requires_key_replays_same_key_and_rejects_competing_key(auth_client):
    project_id = _create_project(auth_client)
    definition = _create_definition(auth_client, project_id)
    run_id = str(_start_run(auth_client, str(definition["id"]))["run"]["id"])

    missing_key = auth_client.raw.post(
        f"/api/v2/workflow-runs/{run_id}/pause",
        headers={"Origin": "http://testserver"},
    )
    assert missing_key.status_code == 422

    first = _control(auth_client, run_id, "pause", "pause-once")
    assert first.status_code == 200, first.text
    replay = _control(auth_client, run_id, "pause", "pause-once")
    assert replay.status_code == 200
    assert replay.json()["run"]["status"] == first.json()["run"]["status"] == "PAUSED"
    assert replay.json().get("idempotent_replay") is True

    conflict = _control(auth_client, run_id, "pause", "pause-again")
    assert conflict.status_code == 409


def test_two_competing_pause_commands_have_exactly_one_winner(auth_client):
    project_id = _create_project(auth_client)
    definition = _create_definition(auth_client, project_id)
    run_id = str(_start_run(auth_client, str(definition["id"]))["run"]["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda key: _control(auth_client, run_id, "pause", key),
                ("pause-race-a", "pause-race-b"),
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    snapshot = auth_client.get(f"/api/v2/workflow-runs/{run_id}").json()
    assert snapshot["run"]["status"] == "PAUSED"


def test_retry_resumes_failed_node_from_checkpoint_and_increments_retry_count(auth_client):
    project_id = _create_project(auth_client)
    definition = _create_definition(auth_client, project_id)
    run_id = str(_start_run(auth_client, str(definition["id"]))["run"]["id"])

    async def mark_failed() -> None:
        async with auth_client.raw.app.state.session_factory() as session:
            node = await session.scalar(
                select(WorkflowNodeStateModel).where(
                    WorkflowNodeStateModel.run_id == run_id,
                    WorkflowNodeStateModel.node_key == "draft",
                )
            )
            assert node is not None
            node.status = "FAILED"
            node.checkpoint_json = '{"cursor": 2}'
            node.retry_count = 1
            from proseforge.infrastructure.database.models.remaining import (
                WorkflowRunModel,
            )

            run = await session.get(WorkflowRunModel, run_id)
            assert run is not None
            run.status = "FAILED"
            await session.commit()

    assert auth_client.raw.portal is not None
    auth_client.raw.portal.call(mark_failed)
    retried = _control(auth_client, run_id, "retry", "retry-draft-once")
    assert retried.status_code == 200, retried.text

    snapshot = auth_client.get(f"/api/v2/workflow-runs/{run_id}").json()
    draft = next(node for node in snapshot["nodes"] if node["node_key"] == "draft")
    assert draft["retry_count"] == 2
    assert draft["status"] in {"PENDING", "RETRYING", "RUNNING"}

    async def checkpoint_after_retry() -> str | None:
        async with auth_client.raw.app.state.session_factory() as session:
            node = await session.scalar(
                select(WorkflowNodeStateModel).where(
                    WorkflowNodeStateModel.run_id == run_id,
                    WorkflowNodeStateModel.node_key == "draft",
                )
            )
            assert node is not None
            return node.checkpoint_json

    assert auth_client.raw.portal.call(checkpoint_after_retry) == '{"cursor": 2}'


def test_budget_blocked_run_does_not_create_a_chapter_version(auth_client):
    project_id = _create_project(auth_client)
    chapter = auth_client.post_json(
        f"/api/v1/projects/{project_id}/chapters",
        {"chapter_no": 1, "title": "Budget guard"},
    )
    assert chapter.status_code == 201
    chapter_id = chapter.json()["id"]
    base = auth_client.post_json(
        f"/api/v1/chapters/{chapter_id}/versions",
        {"content": "Canonical manuscript"},
    )
    assert base.status_code == 201

    blocked_graph = {
        "nodes": [
            {
                "id": "draft",
                "kind": "write",
                "title": "Draft",
                "config": {"chapter_id": chapter_id, "reserved_tokens": 500},
            }
        ],
        "edges": [],
    }
    definition = _create_definition(auth_client, project_id, definition=blocked_graph)
    started = _start_run(auth_client, str(definition["id"]), token_limit=10)
    assert started["run"]["status"] == "BUDGET_BLOCKED"
    assert started["nodes"][0]["reserved_tokens"] == 500

    versions = auth_client.get(f"/api/v1/chapters/{chapter_id}/versions")
    assert versions.status_code == 200
    assert [(item["version_no"], item["content"]) for item in versions.json()] == [
        (1, "Canonical manuscript")
    ]


def test_sse_replays_after_snapshot_cursor_and_finishes_with_persisted_terminal(auth_client):
    project_id = _create_project(auth_client)
    definition = _create_definition(auth_client, project_id)
    run_id = str(_start_run(auth_client, str(definition["id"]))["run"]["id"])
    snapshot = auth_client.get(f"/api/v2/workflow-runs/{run_id}").json()
    cursor = int(snapshot["event_cursor"])

    assert _control(auth_client, run_id, "pause", "sse-pause").status_code == 200
    assert _control(auth_client, run_id, "cancel", "sse-cancel").status_code == 200

    with auth_client.stream(
        "GET",
        f"/api/v2/workflow-runs/{run_id}/events",
        headers={"Last-Event-ID": str(cursor)},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = [line for line in response.iter_lines() if line]

    event_ids = [int(line.removeprefix("id: ")) for line in lines if line.startswith("id: ")]
    event_names = [line.removeprefix("event: ") for line in lines if line.startswith("event: ")]
    assert event_ids == sorted(set(event_ids))
    assert all(event_id > cursor for event_id in event_ids)
    assert event_names[-1] == "run.cancelled"

    refreshed = auth_client.get(f"/api/v2/workflow-runs/{run_id}").json()
    assert refreshed["run"]["status"] == "CANCELLED"
    assert refreshed["event_cursor"] == event_ids[-1]
