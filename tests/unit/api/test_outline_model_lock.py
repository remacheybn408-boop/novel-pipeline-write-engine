"""Outline import locks the writing model; project API exposes the lock.

POST /api/v1/projects/{id}/outlines/import with provider+model wins the
first-come-first-served lock (source "outline_import"); a later import
with a different model is a no-op. The project detail response carries
the flat lock fields for the frontend.
"""

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


def _lock_state(body: dict[str, object]) -> dict[str, object]:
    return {
        "locked": body["model_locked_at"] is not None,
        "provider": body["writing_model_provider"],
        "model": body["writing_model_id"],
        "source": body["model_lock_source"],
    }


def _create_project(client: TestClient, slug: str = "novel-1") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    body = response.json()
    assert _lock_state(body) == {"locked": False, "provider": None, "model": None, "source": None}
    return body["id"]


def _import_outline(client: TestClient, project_id: str, provider: str | None = None, model: str | None = None) -> None:
    payload: dict[str, object] = {"title": "Outline", "content": "卷一：起"}
    if provider:
        payload["provider"] = provider
    if model:
        payload["model"] = model
    response = client.post(f"/api/v1/projects/{project_id}/outlines/import", json=payload)
    assert response.status_code == 201


def test_outline_import_locks_writing_model(client: TestClient):
    project_id = _create_project(client)
    _import_outline(client, project_id, provider="openai", model="gpt-a")

    lock = _lock_state(client.get("/api/v1/projects/novel-1").json())
    assert lock == {"locked": True, "provider": "openai", "model": "gpt-a", "source": "outline_import"}

    # Second import with a different model: first-come-first-served, no change.
    _import_outline(client, project_id, provider="deepseek", model="deepseek-chat")
    lock = _lock_state(client.get("/api/v1/projects/novel-1").json())
    assert lock == {"locked": True, "provider": "openai", "model": "gpt-a", "source": "outline_import"}


def test_outline_import_without_model_does_not_lock(client: TestClient):
    project_id = _create_project(client)
    _import_outline(client, project_id)

    lock = _lock_state(client.get("/api/v1/projects/novel-1").json())
    assert lock["locked"] is False
