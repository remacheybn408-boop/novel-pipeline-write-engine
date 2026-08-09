"""Files API: upload/list accept BOTH project modes (chat attachments ride
the same endpoints); missing/foreign projects still 404."""

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


def test_work_project_lists_files(client: TestClient):
    project_id = _project(client)

    response = client.get(f"/api/v1/projects/{project_id}/files")

    assert response.status_code == 200
    assert response.json() == []


def test_chat_mode_project_lists_files(client: TestClient):
    # Chat-mode projects accept attachments too (composer file upload).
    project_id = _project(client, slug="chat-1", mode="chat")

    assert client.get(f"/api/v1/projects/{project_id}/files").status_code == 200


def test_chat_mode_project_upload_succeeds(client: TestClient):
    project_id = _project(client, slug="chat-2", mode="chat")

    response = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("notes.txt", b"hello attachment", "text/plain")},
    )

    assert response.status_code == 201, response.text
    assert response.json()["filename"] == "notes.txt"
    listed = client.get(f"/api/v1/projects/{project_id}/files").json()
    assert [row["filename"] for row in listed] == ["notes.txt"]


def test_missing_project_list_files_is_404(client: TestClient):
    assert client.get("/api/v1/projects/does-not-exist/files").status_code == 404
