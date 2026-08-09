"""Chapter content endpoint: GET /api/v1/projects/{pid}/chapters/{cid}/content.

Contract consumed by the frontend getChapterContent: response carries
{chapter_id, title, chapter_no, content} from the chapter's active version.
Real app on native sqlite (TestClient + lifespan).
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


def _project_with_chapter(client: TestClient, *, with_version: bool = True) -> tuple[str, str]:
    response = client.post("/api/v1/projects", json={"slug": "proj-content-1", "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    project_id = response.json()["id"]
    response = client.post(f"/api/v1/projects/{project_id}/chapters", json={"chapter_no": 1, "title": "第一章 雨夜"})
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    if with_version:
        response = client.post(f"/api/v1/chapters/{chapter_id}/versions", json={"content": "雨夜，主角回城。"})
        assert response.status_code == 201, response.text
    return project_id, chapter_id


def test_chapter_content_returns_active_version(client: TestClient):
    project_id, chapter_id = _project_with_chapter(client)

    response = client.get(f"/api/v1/projects/{project_id}/chapters/{chapter_id}/content")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "chapter_id": chapter_id,
        "title": "第一章 雨夜",
        "chapter_no": 1,
        "content": "雨夜，主角回城。",
    }


def test_chapter_content_without_version_is_empty(client: TestClient):
    project_id, chapter_id = _project_with_chapter(client, with_version=False)

    response = client.get(f"/api/v1/projects/{project_id}/chapters/{chapter_id}/content")

    assert response.status_code == 200, response.text
    assert response.json()["content"] == ""


def test_chapter_content_unknown_chapter_is_404(client: TestClient):
    project_id, _chapter_id = _project_with_chapter(client)

    assert client.get(f"/api/v1/projects/{project_id}/chapters/does-not-exist/content").status_code == 404


def test_chapter_content_wrong_project_is_404(client: TestClient):
    project_id, chapter_id = _project_with_chapter(client)
    response = client.post("/api/v1/projects", json={"slug": "proj-content-2", "title": "Other", "mode": "work"})
    other_project_id = response.json()["id"]

    # The chapter exists but belongs to another project: no cross-project leak.
    assert client.get(f"/api/v1/projects/{other_project_id}/chapters/{chapter_id}/content").status_code == 404
    assert project_id != other_project_id
