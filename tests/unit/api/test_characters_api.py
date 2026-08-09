"""Character CRUD API: create/list/patch/delete, 409 on duplicate name,
404 across owners and chat-mode projects, PATCH promotes source to user."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
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


def test_crud_roundtrip(client: TestClient):
    project_id = _project(client)
    response = client.post(f"/api/v1/projects/{project_id}/characters", json={
        "name": "李雷", "aliases": ["雷子"], "summary": "主角", "role": "主角",
    })
    assert response.status_code == 201
    character = response.json()
    assert character["source"] == "user" and character["aliases"] == ["雷子"]

    # Duplicate name -> 409.
    response = client.post(f"/api/v1/projects/{project_id}/characters", json={"name": "李雷"})
    assert response.status_code == 409

    listing = client.get(f"/api/v1/projects/{project_id}/characters").json()
    assert [c["name"] for c in listing] == ["李雷"]

    # PATCH: edit promotes source to user even for auto rows.
    response = client.patch(f"/api/v1/projects/{project_id}/characters/{character['id']}", json={
        "summary": "改过的简介", "aliases": ["雷子", "小李"],
    })
    assert response.status_code == 200
    updated = response.json()
    assert updated["summary"] == "改过的简介"
    assert updated["aliases"] == ["雷子", "小李"]
    assert updated["source"] == "user"

    # Archive then list filters.
    response = client.patch(f"/api/v1/projects/{project_id}/characters/{character['id']}", json={"status": "archived"})
    assert response.status_code == 200
    assert client.get(f"/api/v1/projects/{project_id}/characters").json() == []
    assert len(client.get(f"/api/v1/projects/{project_id}/characters", params={"include_archived": True}).json()) == 1

    # DELETE hard-deletes.
    assert client.delete(f"/api/v1/projects/{project_id}/characters/{character['id']}").status_code == 204
    assert client.delete(f"/api/v1/projects/{project_id}/characters/{character['id']}").status_code == 404


def test_missing_character_is_404(client: TestClient):
    project_id = _project(client)
    assert client.patch(f"/api/v1/projects/{project_id}/characters/nope", json={"summary": "x"}).status_code == 404
    assert client.delete(f"/api/v1/projects/{project_id}/characters/nope").status_code == 404


def test_chat_mode_project_is_404(client: TestClient):
    project_id = _project(client, slug="chat-1", mode="chat")
    assert client.get(f"/api/v1/projects/{project_id}/characters").status_code == 404
    assert client.post(f"/api/v1/projects/{project_id}/characters", json={"name": "甲"}).status_code == 404


def test_foreign_project_is_404(client: TestClient):
    assert client.get("/api/v1/projects/does-not-exist/characters").status_code == 404


def test_characters_require_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/projects/whatever/characters").status_code == 401


def test_scene_state_endpoint(client: TestClient):
    project_id = _project(client, slug="novel-2")
    response = client.get(f"/api/v1/projects/{project_id}/scene-state")
    assert response.status_code == 200
    body = response.json()
    assert body["latest_chapter"] is None
    assert body["recent_summaries"] == []
    assert client.get("/api/v1/projects/does-not-exist/scene-state").status_code == 404
