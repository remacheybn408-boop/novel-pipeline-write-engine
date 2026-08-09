"""Canon conflicts API: list open, resolve with note, 404s, 401."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.retrieval import CanonConflictModel
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


def _project(client: TestClient, slug: str = "novel-1", mode: str = "work") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": mode})
    assert response.status_code == 201
    return response.json()["id"]


def _seed_conflict(client: TestClient, project_id: str, conflict_id: str = "cf1") -> None:
    async def _seed() -> None:
        async with client.app.state.session_factory() as session:
            session.add(CanonConflictModel(
                id=conflict_id, project_id=project_id,
                candidate_source="chapter_version:v1", conflicting_source="character:c1",
                field_or_claim="李雷 的 role（角色定位）",
                evidence_json=json.dumps({"candidate_value": "反派", "existing_value": "主角", "chapter_no": 3}),
                status="open",
            ))
            await session.commit()

    asyncio.run(_seed())


def test_list_open_conflicts_with_evidence(client: TestClient):
    project_id = _project(client)
    _seed_conflict(client, project_id)

    rows = client.get(f"/api/v1/projects/{project_id}/conflicts").json()
    assert len(rows) == 1
    assert rows[0]["field_or_claim"].startswith("李雷")
    assert rows[0]["evidence"]["candidate_value"] == "反派"
    assert rows[0]["status"] == "open"


def test_resolve_marks_resolved_and_records_note(client: TestClient):
    project_id = _project(client)
    _seed_conflict(client, project_id)

    response = client.post(f"/api/v1/projects/{project_id}/conflicts/cf1/resolve", json={"resolution": "以设定为准"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolved_by"]
    assert body["evidence"]["resolution"] == "以设定为准"
    # No longer listed as open; visible under status=resolved.
    assert client.get(f"/api/v1/projects/{project_id}/conflicts").json() == []
    rows = client.get(f"/api/v1/projects/{project_id}/conflicts", params={"status": "resolved"}).json()
    assert len(rows) == 1


def test_conflicts_404s(client: TestClient):
    project_id = _project(client)
    assert client.post(f"/api/v1/projects/{project_id}/conflicts/nope/resolve", json={}).status_code == 404
    assert client.get("/api/v1/projects/does-not-exist/conflicts").status_code == 404
    chat_id = _project(client, slug="chat-1", mode="chat")
    assert client.get(f"/api/v1/projects/{chat_id}/conflicts").status_code == 404


def test_conflicts_require_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/projects/whatever/conflicts").status_code == 401
