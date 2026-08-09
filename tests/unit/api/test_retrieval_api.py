"""Retrieval citation API + story-bible exclude toggle.

Covers GET /api/v1/conversations/{cid}/messages/{mid}/retrieval
(snapshot fields, title join, 404 for foreign/missing, 401 anonymous)
and POST /api/v2/story-bible/{entry_id}/exclude (toggle excluded and
restore semantics for plain facts vs promises).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.conversation import ConversationModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalRunModel,
)
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


def _project(client: TestClient, slug: str = "novel-1") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    return response.json()["id"]


def _seed_snapshot(client: TestClient, project_id: str, conversation_id: str = "conv1", message_id: str = "msg1") -> None:
    async def _seed() -> None:
        now = datetime.now(UTC)
        async with client.app.state.session_factory() as session:
            session.add(ConversationModel(id=conversation_id, project_id=project_id, title="会话"))
            session.add(RetrievalDocumentModel(
                id="doc1", project_id=project_id, source_type="chapter", source_id="ch1",
                source_version="v1", title="第三章 雨夜", created_at=now, updated_at=now,
            ))
            await session.flush()
            session.add(RetrievalChunkModel(
                id="ck1", project_id=project_id, document_id="doc1", chunk_index=0,
                content="雨夜的正文片段", content_hash="h1", created_at=now, updated_at=now,
            ))
            session.add(RetrievalRunModel(
                id="run1", project_id=project_id, conversation_id=conversation_id, message_id=message_id,
                query_text="雨夜发生了什么", intent="story_so_far",
                selected_chunks_json=json.dumps({
                    "chunks": [{"chunk_id": "ck1", "score": 0.87, "chapter_no": 3, "expanded": True}],
                    "trimmed": [{"section": "story_bible", "item": "timeline:旧誓", "reason": "budget"}],
                    "budget": {"structured_tokens": 900, "evidence_tokens": 300, "token_cost": 1200},
                }, ensure_ascii=False),
                elapsed_ms=12.5, token_cost=1200, created_at=now,
            ))
            await session.commit()

    asyncio.run(_seed())


def test_retrieval_snapshot_returns_chunks_titles_trimmed_and_budget(client: TestClient):
    project_id = _project(client)
    _seed_snapshot(client, project_id)

    response = client.get("/api/v1/conversations/conv1/messages/msg1/retrieval")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "run1"
    assert body["query_text"] == "雨夜发生了什么"
    assert body["intent"] == "story_so_far"
    assert body["elapsed_ms"] == 12.5
    assert body["token_cost"] == 1200
    assert body["chunks"] == [
        {"chunk_id": "ck1", "chapter_no": 3, "document_title": "第三章 雨夜", "score": 0.87, "expanded": True}
    ]
    assert body["trimmed"] == [{"section": "story_bible", "item": "timeline:旧誓", "reason": "budget"}]
    assert body["budget"]["evidence_tokens"] == 300


def test_retrieval_snapshot_legacy_bare_list_is_normalized(client: TestClient):
    project_id = _project(client)

    async def _seed() -> None:
        now = datetime.now(UTC)
        async with client.app.state.session_factory() as session:
            session.add(ConversationModel(id="conv1", project_id=project_id, title="会话"))
            session.add(RetrievalRunModel(
                id="run1", project_id=project_id, conversation_id="conv1", message_id="msg1",
                query_text="q", intent="story_so_far",
                selected_chunks_json=json.dumps([{"chunk_id": "ck-x", "score": 0.5, "chapter_no": 1}]),
                elapsed_ms=1.0, token_cost=10, created_at=now,
            ))
            await session.commit()

    asyncio.run(_seed())

    body = client.get("/api/v1/conversations/conv1/messages/msg1/retrieval").json()
    assert body["chunks"][0]["chunk_id"] == "ck-x"
    assert body["chunks"][0]["document_title"] == ""  # chunk not in DB anymore
    assert body["trimmed"] == []
    assert body["budget"] == {}


def test_retrieval_snapshot_404s(client: TestClient):
    project_id = _project(client)
    _seed_snapshot(client, project_id)

    # Message without a snapshot: uniform 404 (missing sub-resource).
    assert client.get("/api/v1/conversations/conv1/messages/other/retrieval").status_code == 404
    # Conversation that does not exist.
    assert client.get("/api/v1/conversations/nope/messages/msg1/retrieval").status_code == 404

    # Foreign user's conversation: also 404, never a leak of existence.
    async def _seed_foreign() -> None:
        async with client.app.state.session_factory() as session:
            session.add(UserModel(id="u2", email="other@example.com", password_hash="x"))
            session.add(ProjectModel(id="p2", owner_id="u2", slug="other", title="Other"))
            await session.flush()
            session.add(ConversationModel(id="conv2", project_id="p2", title="别人的会话"))
            await session.commit()

    asyncio.run(_seed_foreign())
    assert client.get("/api/v1/conversations/conv2/messages/msg1/retrieval").status_code == 404


def test_retrieval_snapshot_requires_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/conversations/c/messages/m/retrieval").status_code == 401


def _create_fact(client: TestClient, project_id: str, kind: str = "world_rule", key: str = "重力法则") -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/story-bible/entries",
        json={"kind": kind, "key": key, "value": {"triggers": [key]}},
    )
    assert response.status_code == 201
    return response.json()


def test_exclude_toggle_excludes_and_restores_fact(client: TestClient):
    project_id = _project(client)
    fact = _create_fact(client, project_id)
    assert fact["status"] == "active"

    excluded = client.post(f"/api/v2/story-bible/{fact['id']}/exclude")
    assert excluded.status_code == 200
    assert excluded.json()["status"] == "excluded"
    assert excluded.json()["version"] == fact["version"] + 1

    restored = client.post(f"/api/v2/story-bible/{fact['id']}/exclude")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"


def test_exclude_restore_returns_promise_to_open(client: TestClient):
    project_id = _project(client)
    fact = _create_fact(client, project_id, kind="promise", key="主角的誓言")
    assert fact["status"] == "open"

    assert client.post(f"/api/v2/story-bible/{fact['id']}/exclude").json()["status"] == "excluded"
    restored = client.post(f"/api/v2/story-bible/{fact['id']}/exclude")
    assert restored.json()["status"] == "open"  # promises restore to open, not active


def test_exclude_unknown_fact_404(client: TestClient):
    _project(client)
    assert client.post("/api/v2/story-bible/does-not-exist/exclude").status_code == 404
