"""Knowledge base CRUD API: list/create/get/patch/delete, owner isolation
(404 across users and chat-mode projects), auth required."""

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
        data_dir=str(tmp_path / "data"),
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
    response = client.post(
        f"/api/v1/projects/{project_id}/knowledge-base",
        json={"title": "世界观设定", "content": "大陆编年史……"},
    )
    assert response.status_code == 201
    document = response.json()
    assert document["title"] == "世界观设定"
    assert document["created_at"] is not None

    listing = client.get(f"/api/v1/projects/{project_id}/knowledge-base").json()
    assert [doc["id"] for doc in listing] == [document["id"]]

    fetched = client.get(f"/api/v1/projects/{project_id}/knowledge-base/{document['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "大陆编年史……"

    response = client.patch(
        f"/api/v1/projects/{project_id}/knowledge-base/{document['id']}",
        json={"title": "新标题", "content": "新内容"},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "新标题"

    assert client.delete(f"/api/v1/projects/{project_id}/knowledge-base/{document['id']}").status_code == 204
    assert client.get(f"/api/v1/projects/{project_id}/knowledge-base/{document['id']}").status_code == 404


def test_blank_title_is_rejected(client: TestClient):
    """Whitespace-only titles pass pydantic min_length=1 but must not reach
    the database: create and update both answer 422."""
    project_id = _project(client)
    response = client.post(
        f"/api/v1/projects/{project_id}/knowledge-base",
        json={"title": "   ", "content": "正文"},
    )
    assert response.status_code == 422

    created = client.post(
        f"/api/v1/projects/{project_id}/knowledge-base",
        json={"title": "设定集"},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]

    response = client.patch(
        f"/api/v1/projects/{project_id}/knowledge-base/{document_id}",
        json={"title": "  \t "},
    )
    assert response.status_code == 422
    # The rejected update leaves the stored title untouched.
    fetched = client.get(f"/api/v1/projects/{project_id}/knowledge-base/{document_id}")
    assert fetched.json()["title"] == "设定集"


def test_foreign_project_is_404(client: TestClient):
    assert client.get("/api/v1/projects/does-not-exist/knowledge-base").status_code == 404
    assert (
        client.post("/api/v1/projects/does-not-exist/knowledge-base", json={"title": "x"}).status_code == 404
    )


def test_chat_mode_project_is_404(client: TestClient):
    project_id = _project(client, slug="chat-1", mode="chat")
    assert client.get(f"/api/v1/projects/{project_id}/knowledge-base").status_code == 404
    assert client.post(f"/api/v1/projects/{project_id}/knowledge-base", json={"title": "x"}).status_code == 404


def test_other_users_project_is_404(client: TestClient):
    project_id = _project(client)
    response = client.post(f"/api/v1/projects/{project_id}/knowledge-base", json={"title": "私有设定"})
    assert response.status_code == 201
    document_id = response.json()["id"]

    # Register a second user directly, then log in as them on the same client.
    import asyncio

    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

    async def _create_other_user() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            await uow.users.create("other@example.com", client.app.state.auth.hash_password("q" * 12), "USER")
            await uow.commit()

    asyncio.run(_create_other_user())
    response = client.post("/api/v1/auth/login", json={"email": "other@example.com", "password": "q" * 12})
    assert response.status_code == 200

    assert client.get(f"/api/v1/projects/{project_id}/knowledge-base").status_code == 404
    assert client.get(f"/api/v1/projects/{project_id}/knowledge-base/{document_id}").status_code == 404
    assert (
        client.patch(f"/api/v1/projects/{project_id}/knowledge-base/{document_id}", json={"title": "篡改"}).status_code
        == 404
    )
    assert client.delete(f"/api/v1/projects/{project_id}/knowledge-base/{document_id}").status_code == 404


def test_knowledge_requires_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        data_dir=str(tmp_path / "data"),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/projects/whatever/knowledge-base").status_code == 401
