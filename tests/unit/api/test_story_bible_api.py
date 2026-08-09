"""Story-bible API: work-only mode assertion — chat-mode projects get the
same 404 as missing/foreign projects, work projects read/write normally."""

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


def _fact_payload() -> dict:
    return {"kind": "character", "key": "Mira", "value": {"triggers": ["Mira"], "budget_tokens": 24}}


def test_work_project_can_create_and_list_facts(client: TestClient):
    project_id = _project(client)

    created = client.post(f"/api/v2/projects/{project_id}/story-bible/entries", json=_fact_payload())
    assert created.status_code == 201

    listed = client.get(f"/api/v2/projects/{project_id}/story-bible")
    assert listed.status_code == 200
    assert listed.json() == [created.json()]


def test_chat_mode_project_is_404(client: TestClient):
    project_id = _project(client, slug="chat-1", mode="chat")

    assert client.get(f"/api/v2/projects/{project_id}/story-bible").status_code == 404
    assert client.post(f"/api/v2/projects/{project_id}/story-bible/entries", json=_fact_payload()).status_code == 404


def test_missing_project_is_404(client: TestClient):
    assert client.get("/api/v2/projects/does-not-exist/story-bible").status_code == 404
