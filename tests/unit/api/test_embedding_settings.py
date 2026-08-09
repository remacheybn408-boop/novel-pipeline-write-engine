"""GET/PUT /api/v1/settings/embedding — three-engine embedding config.

Runs the real app on native sqlite (TestClient + lifespan). Covers: the
local default, legacy (engine-less) preferences reading as "api", the
whitelist/credential 400s, the 409 identity conflict, and the force=true
clear + reindex flow (queue replaced with a recording stub).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.application.retrieval.indexing import (
    EMBEDDING_NOT_CONFIGURED_ERROR,
    REBUILD_PREF_KEY,
)
from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.chapter import (
    ChapterModel,
    ChapterVersionModel,
)
from proseforge.infrastructure.database.models.plugin import UserPreferenceModel
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.retrieval import (
    RetrievalChunkModel,
    RetrievalDocumentModel,
    RetrievalJobModel,
)
from proseforge.infrastructure.embeddings.local import DEFAULT_LOCAL_MODEL
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
# A hidden-but-still-whitelisted fastembed model: exercises the download
# endpoint against LocalEmbedder (the default bge-m3 goes to llama.cpp).
FASTEMBED_MODEL = "BAAI/bge-small-zh-v1.5"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        embedding_cache_dir=str(tmp_path / "embeddings"),
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        yield test_client


def _create_credential(client: TestClient, provider: str = "openai") -> None:
    response = client.post("/api/v1/credentials", json={"provider": provider, "api_key": "sk-test-1234567890"})
    assert response.status_code == 201


async def _owner_id(client: TestClient) -> str:
    from sqlalchemy import select

    async with client.app.state.session_factory() as session:
        return (await session.scalars(select(UserModel).where(UserModel.email == "owner@example.com"))).one().id


def _seed_preference(client: TestClient, value: dict) -> None:
    """Write the embedding preference directly, bypassing the PUT validation."""

    async def _seed() -> None:
        async with client.app.state.session_factory() as session:
            session.add(UserPreferenceModel(
                id="pref-1", user_id=await _owner_id(client), key="embedding",
                value_json=json.dumps(value), updated_at=datetime.now(UTC),
            ))
            await session.commit()

    asyncio.run(_seed())


def _seed_chunk(client: TestClient, embedding_model: str) -> None:
    """Seed one active indexed chunk owned by the setup user."""

    async def _seed() -> None:
        now = datetime.now(UTC)
        async with client.app.state.session_factory() as session:
            user_id = await _owner_id(client)
            session.add(ProjectModel(id="proj-1", owner_id=user_id, slug="novel", title="Novel"))
            await session.flush()
            session.add(RetrievalDocumentModel(
                id="doc-1", project_id="proj-1", source_type="chapter", source_id="chap-1",
                source_version="ver-1", title="第一章", status="active", authority_level="canon",
                created_at=now, updated_at=now,
            ))
            await session.flush()
            session.add(RetrievalChunkModel(
                id="chunk-1", project_id="proj-1", document_id="doc-1", chunk_index=0,
                content="正文", summary="", metadata_json="{}", search_text="正文",
                embedding=[0.1, 0.2], embedding_model=embedding_model, embedding_version="v1",
                token_count=1, content_hash="h", status="active", created_at=now, updated_at=now,
            ))
            await session.commit()

    asyncio.run(_seed())


def _seed_chapter_with_active_version(client: TestClient) -> None:
    """Seed a work-mode chapter (plus a chat-mode one) for the reindex flow."""

    async def _seed() -> None:
        async with client.app.state.session_factory() as session:
            user_id = await _owner_id(client)
            session.add(ChapterModel(
                id="chap-1", project_id="proj-1", chapter_no=1, title="第一章",
                status="DONE", active_version_id="ver-1",
            ))
            session.add(ChapterVersionModel(
                id="ver-1", chapter_id="chap-1", version_no=1, content="正文。",
                content_hash="h1", word_count=3,
            ))
            # Chat-mode project: must NOT be picked up by the reindex sweep.
            session.add(ProjectModel(id="proj-chat", owner_id=user_id, slug="chat", title="Chat", mode="chat"))
            await session.flush()
            session.add(ChapterModel(
                id="chap-chat", project_id="proj-chat", chapter_no=1, title="闲聊",
                status="DONE", active_version_id="ver-chat",
            ))
            session.add(ChapterVersionModel(
                id="ver-chat", chapter_id="chap-chat", version_no=1, content="闲聊。",
                content_hash="h2", word_count=3,
            ))
            await session.commit()

    asyncio.run(_seed())


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def enqueue(self, task_name: str, payload: dict) -> str:
        self.calls.append((task_name, payload))
        return f"task-{len(self.calls)}"


def test_get_default_is_local_engine(client: TestClient):
    response = client.get("/api/v1/settings/embedding")
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "local"
    assert body["provider"] is None and body["model"] is None
    assert body["local_model"] == DEFAULT_LOCAL_MODEL == "BAAI/bge-m3"
    assert body["local"]["status"] == "not_downloaded"
    assert body["local"]["model"] == DEFAULT_LOCAL_MODEL
    assert body["local"]["dimension"] == 1024
    assert body["indexed_model"] is None


def test_get_exposes_only_visible_models(client: TestClient, tmp_path):
    """bge-m3 convergence: local_models/visible_models expose only the
    visible registry entries. Hidden models stay downloadable/usable for
    rollback but their disk status is not offered to the UI."""
    cache_dir = tmp_path / "embeddings"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "status.intfloat--multilingual-e5-large.json").write_text(
        json.dumps({"state": "ready", "model": "intfloat/multilingual-e5-large"}),
        encoding="utf-8",
    )
    body = client.get("/api/v1/settings/embedding").json()
    # Only bge-m3 is visible; the hidden e5 disk-truth file is not reported.
    assert set(body["local_models"]) == {"BAAI/bge-m3"}
    assert body["local_models"]["BAAI/bge-m3"]["status"] == "not_downloaded"
    visible = {item["id"]: item for item in body["visible_models"]}
    assert set(visible) == {"BAAI/bge-m3"}
    assert visible["BAAI/bge-m3"]["dimension"] == 1024
    assert visible["BAAI/bge-m3"]["chunk_chars"] == 1200


def test_legacy_preference_without_engine_reads_as_api(client: TestClient):
    _seed_preference(client, {"provider": "openai", "model": "embed-1"})
    body = client.get("/api/v1/settings/embedding").json()
    assert body["engine"] == "api"
    assert body["provider"] == "openai" and body["model"] == "embed-1"
    assert body["local_model"] == DEFAULT_LOCAL_MODEL


def test_put_api_without_credential_is_400(client: TestClient):
    response = client.put("/api/v1/settings/embedding", json={"engine": "api", "provider": "openai", "model": "embed-1"})
    assert response.status_code == 400


def test_put_api_missing_provider_is_400(client: TestClient):
    response = client.put("/api/v1/settings/embedding", json={"engine": "api", "model": "embed-1"})
    assert response.status_code == 400


def test_put_local_model_outside_whitelist_is_400(client: TestClient):
    response = client.put("/api/v1/settings/embedding", json={"engine": "local", "local_model": "foo/bar"})
    assert response.status_code == 400


def test_put_api_roundtrip(client: TestClient):
    _create_credential(client)
    response = client.put("/api/v1/settings/embedding", json={"engine": "api", "provider": "openai", "model": "embed-1"})
    assert response.status_code == 200
    assert response.json()["engine"] == "api"
    body = client.get("/api/v1/settings/embedding").json()
    assert body["engine"] == "api" and body["provider"] == "openai" and body["model"] == "embed-1"


def test_put_off_roundtrip(client: TestClient):
    response = client.put("/api/v1/settings/embedding", json={"engine": "off"})
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "off" and body["provider"] is None and body["model"] is None
    assert client.get("/api/v1/settings/embedding").json()["engine"] == "off"


def test_put_same_identity_with_existing_chunks_ok(client: TestClient):
    _create_credential(client)
    _seed_chunk(client, "openai/embed-1")
    response = client.put("/api/v1/settings/embedding", json={"engine": "api", "provider": "openai", "model": "embed-1"})
    assert response.status_code == 200
    assert response.json()["indexed_model"] == "openai/embed-1"


def test_put_different_identity_with_existing_chunks_is_409(client: TestClient):
    _create_credential(client)
    _seed_chunk(client, "openai/embed-1")
    response = client.put("/api/v1/settings/embedding", json={"engine": "api", "provider": "openai", "model": "embed-2"})
    assert response.status_code == 409
    # Config must not have changed: still the local default.
    assert client.get("/api/v1/settings/embedding").json()["engine"] == "local"


def test_put_force_clears_chunks_and_enqueues_reindex(client: TestClient):
    _create_credential(client)
    _seed_chunk(client, "openai/embed-1")
    _seed_chapter_with_active_version(client)
    queue = _RecordingQueue()
    client.app.state.queue = queue

    response = client.put("/api/v1/settings/embedding", json={"engine": "off", "force": True})

    assert response.status_code == 200
    assert response.json()["engine"] == "off"
    assert response.json()["indexed_model"] is None

    async def _check() -> None:
        from sqlalchemy import select

        async with client.app.state.session_factory() as session:
            assert list(await session.scalars(select(RetrievalChunkModel))) == []
            assert list(await session.scalars(select(RetrievalDocumentModel))) == []
            jobs = list(await session.scalars(select(RetrievalJobModel)))
            # Only the work-mode chapter is re-enqueued, not the chat-mode one.
            assert [(job.project_id, job.source_id, job.status) for job in jobs] == [("proj-1", "chap-1", "pending")]

    asyncio.run(_check())
    assert len(queue.calls) == 1
    task_name, payload = queue.calls[0]
    assert task_name == "proseforge.retrieval.index_document"
    assert payload["user_id"]
    assert set(payload) == {"job_id", "user_id"}
    # New preference is live.
    assert client.get("/api/v1/settings/embedding").json()["engine"] == "off"


def _seed_job(client: TestClient, job_id: str, *, status: str, error: str | None = None) -> None:
    """Seed one retrieval job on proj-1 (requires _seed_chunk's project)."""

    async def _seed() -> None:
        async with client.app.state.session_factory() as session:
            session.add(RetrievalJobModel(
                id=job_id, project_id="proj-1", job_type="index_chapter", source_type="chapter",
                source_id="chap-1", status=status, attempt=1, error=error,
                requested_at=datetime.now(UTC),
            ))
            await session.commit()

    asyncio.run(_seed())


def test_put_force_deletes_unfinished_jobs(client: TestClient):
    """force 重建必须删掉在途 job：否则 running job 会以旧引擎身份重写
    document 且 source_version 命中 skip，章节静默脱库。"""
    _create_credential(client)
    _seed_chunk(client, "openai/embed-1")
    _seed_chapter_with_active_version(client)
    _seed_job(client, "job-pending", status="pending")
    _seed_job(client, "job-running", status="running")
    _seed_job(client, "job-done", status="done")
    client.app.state.queue = _RecordingQueue()

    response = client.put("/api/v1/settings/embedding", json={"engine": "off", "force": True})

    assert response.status_code == 200

    async def _check() -> None:
        from sqlalchemy import select

        async with client.app.state.session_factory() as session:
            jobs = list(await session.scalars(select(RetrievalJobModel)))
            by_id = {job.id: job.status for job in jobs}
            assert "job-pending" not in by_id
            assert "job-running" not in by_id
            assert by_id["job-done"] == "done"
            # The force rebuild still enqueues its fresh replacement job.
            assert [status for job_id, status in by_id.items() if job_id != "job-done"] == ["pending"]

    asyncio.run(_check())


def test_put_requeues_all_failed_index_jobs(client: TestClient):
    """保存 embedding 设置成功后，该用户所有 failed 的 index_chapter job
    都被重置为 pending（不限于「embedding 未配置」——保存也可能修好了
    模型下载、llama-server 端口等任意一类原因），由 sweeper 自然捞起。"""
    _seed_chunk(client, "openai/embed-1")  # provides proj-1; chunk matches the saved identity below
    _seed_job(client, "job-cfg", status="failed", error=EMBEDDING_NOT_CONFIGURED_ERROR)
    _seed_job(client, "job-other", status="failed", error="EmbeddingError: upstream down")
    _create_credential(client)

    response = client.put(
        "/api/v1/settings/embedding",
        json={"engine": "api", "provider": "openai", "model": "embed-1"},
    )

    assert response.status_code == 200

    async def _check() -> None:
        async with client.app.state.session_factory() as session:
            for job_id in ("job-cfg", "job-other"):
                job = await session.get(RetrievalJobModel, job_id)
                assert job.status == "pending" and job.error is None

    asyncio.run(_check())


def test_put_force_rebuilds_even_with_empty_index(client: TestClient):
    """空库（全部索引任务失败、没有任何 chunks）时 conflicts=0，旧逻辑下
    force 是空操作——而空库恰恰是必须重建的状态。force 现在无条件全量重建。"""
    _seed_chunk(client, "openai/embed-1")  # provides proj-1 + chap-1's project
    _seed_chapter_with_active_version(client)
    queue = _RecordingQueue()
    client.app.state.queue = queue

    async def _clear_chunks() -> None:
        from sqlalchemy import delete

        async with client.app.state.session_factory() as session:
            await session.execute(delete(RetrievalChunkModel))
            await session.execute(delete(RetrievalDocumentModel))
            await session.commit()

    asyncio.run(_clear_chunks())

    response = client.put("/api/v1/settings/embedding", json={"engine": "local", "force": True})

    assert response.status_code == 200
    assert len(queue.calls) == 1  # reindex job enqueued despite zero conflicts


def _set_job_status(client: TestClient, job_id: str, status: str) -> None:
    async def _update() -> None:
        async with client.app.state.session_factory() as session:
            job = await session.get(RetrievalJobModel, job_id)
            job.status = status
            await session.commit()

    asyncio.run(_update())


def _rebuild_marker_state(client: TestClient) -> str | None:
    async def _read() -> str | None:
        from sqlalchemy import select

        async with client.app.state.session_factory() as session:
            marker = await session.scalar(
                select(UserPreferenceModel).where(
                    UserPreferenceModel.user_id == await _owner_id(client),
                    UserPreferenceModel.key == REBUILD_PREF_KEY,
                )
            )
            return json.loads(marker.value_json)["state"] if marker else None

    return asyncio.run(_read())


def test_force_rebuild_suppresses_drift_alarm(client: TestClient):
    """force 清空重建期间 index_health 必然 drift——rebuild 标记落库后，
    只要在途 job 未完结，告警必须静默（drift=False + rebuilding=True）。"""
    _seed_chunk(client, "openai/embed-1")  # provides proj-1
    _seed_chapter_with_active_version(client)  # one indexable chapter
    client.app.state.queue = _RecordingQueue()

    response = client.put("/api/v1/settings/embedding", json={"engine": "off", "force": True})

    assert response.status_code == 200
    health = response.json()["index_health"]
    # Mid-rebuild: the chapter is not yet re-indexed, but this drift is by
    # design and must not alarm.
    assert health["indexable_chapters"] == 1
    assert health["indexed_documents"] == 0
    assert health["drift"] is False
    assert health["rebuilding"] is True
    assert _rebuild_marker_state(client) == "rebuilding"


def test_drift_alarm_returns_when_rebuild_stalls(client: TestClient):
    """在途 job 全部终态（重建失败/停滞）后标记失效：drift 重新上报警。"""
    _seed_chunk(client, "openai/embed-1")
    _seed_chapter_with_active_version(client)
    client.app.state.queue = _RecordingQueue()
    response = client.put("/api/v1/settings/embedding", json={"engine": "off", "force": True})
    assert response.json()["index_health"]["rebuilding"] is True

    # The rebuild's only job dies permanently: the empty index is no longer
    # "rebuilding", it is genuinely drifted.
    job_id = client.app.state.queue.calls[0][1]["job_id"]
    _set_job_status(client, job_id, "failed")

    health = client.get("/api/v1/settings/embedding").json()["index_health"]
    assert health["drift"] is True
    assert "rebuilding" not in health
    assert _rebuild_marker_state(client) == "done"


def _seed_document_and_chunk(client: TestClient, embedding_model: str) -> None:
    """Re-seed doc-1/chunk-1 on the existing proj-1 (post-force-rebuild state)."""

    async def _seed() -> None:
        now = datetime.now(UTC)
        async with client.app.state.session_factory() as session:
            session.add(RetrievalDocumentModel(
                id="doc-1", project_id="proj-1", source_type="chapter", source_id="chap-1",
                source_version="ver-1", title="第一章", status="active", authority_level="canon",
                created_at=now, updated_at=now,
            ))
            await session.flush()
            session.add(RetrievalChunkModel(
                id="chunk-1", project_id="proj-1", document_id="doc-1", chunk_index=0,
                content="正文", summary="", metadata_json="{}", search_text="正文",
                embedding=[0.1, 0.2], embedding_model=embedding_model, embedding_version="v1",
                token_count=1, content_hash="h", status="active", created_at=now, updated_at=now,
            ))
            await session.commit()

    asyncio.run(_seed())


def test_rebuild_marker_cleared_after_drift_resolves(client: TestClient):
    """重建完成（索引回到一致）后标记清除，health 如实回报无 drift。"""
    _seed_chunk(client, "openai/embed-1")
    _seed_chapter_with_active_version(client)
    client.app.state.queue = _RecordingQueue()
    response = client.put("/api/v1/settings/embedding", json={"engine": "off", "force": True})
    assert response.json()["index_health"]["rebuilding"] is True

    # Simulate the re-index finishing: the chapter is indexed again under the
    # new identity and no jobs remain in flight.
    job_id = client.app.state.queue.calls[0][1]["job_id"]
    _set_job_status(client, job_id, "done")
    _seed_document_and_chunk(client, "none")

    health = client.get("/api/v1/settings/embedding").json()["index_health"]
    assert health["drift"] is False
    assert "rebuilding" not in health
    assert _rebuild_marker_state(client) == "done"


def test_settings_requires_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        embedding_cache_dir=str(tmp_path / "embeddings"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/settings/embedding").status_code == 401
        assert anonymous.post(
            "/api/v1/settings/embedding/download", json={"local_model": DEFAULT_LOCAL_MODEL}
        ).status_code == 401


# --- POST /settings/embedding/download (background model download) ---


class _FakeEmbedder:
    """Stands in for LocalEmbedder: records construction, never downloads."""

    instances: ClassVar[list[_FakeEmbedder]] = []

    def __init__(self, model, **kwargs):
        self.model = model
        self.kwargs = kwargs
        type(self).instances.append(self)

    async def ensure_ready(self):
        return None


@pytest.fixture()
def fake_embedder(monkeypatch):
    from proseforge.api.routes import embedding_settings

    _FakeEmbedder.instances = []
    monkeypatch.setattr(embedding_settings, "LocalEmbedder", _FakeEmbedder)
    return _FakeEmbedder


def test_download_starts_background_task(client: TestClient, fake_embedder):
    response = client.post("/api/v1/settings/embedding/download", json={"local_model": FASTEMBED_MODEL})

    assert response.status_code == 202
    assert response.json()["model"] == FASTEMBED_MODEL
    assert len(fake_embedder.instances) == 1
    assert fake_embedder.instances[0].model == FASTEMBED_MODEL


def test_download_is_idempotent(client: TestClient, fake_embedder):
    first = client.post("/api/v1/settings/embedding/download", json={"local_model": FASTEMBED_MODEL})
    second = client.post("/api/v1/settings/embedding/download", json={"local_model": FASTEMBED_MODEL})

    # Repeat calls return 202 again; the download lock makes them no-ops.
    assert first.status_code == 202 and second.status_code == 202


def test_download_rejects_model_outside_whitelist(client: TestClient, fake_embedder):
    response = client.post("/api/v1/settings/embedding/download", json={"local_model": "foo/bar"})

    assert response.status_code == 422
    assert fake_embedder.instances == []


# --- Dedicated embedding credential (independent API base/key) ---


def test_embedding_identity_includes_base_url_host():
    from proseforge.application.retrieval.indexing import (
        embedding_identity,
        normalize_embedding_preference,
    )

    config = normalize_embedding_preference({
        "engine": "api",
        "provider": "openai",
        "model": "embed-1",
        "credential_provider": "embedding:openai",
        "base_url": "https://embed.example.com/v1",
    })
    assert config["credential_provider"] == "embedding:openai"
    assert config["base_url"] == "https://embed.example.com/v1"
    # The host joins the identity so a base switch trips the 409 reindex guard.
    assert embedding_identity(config) == "openai/embed-1@embed.example.com"

    legacy = normalize_embedding_preference({"engine": "api", "provider": "openai", "model": "embed-1"})
    assert legacy["credential_provider"] is None and legacy["base_url"] is None
    assert embedding_identity(legacy) == "openai/embed-1"


def test_put_api_with_independent_credential_roundtrip(client: TestClient):
    # No chat credential configured: the dedicated key alone must suffice.
    response = client.put(
        "/api/v1/settings/embedding",
        json={
            "engine": "api",
            "provider": "openai",
            "model": "embed-1",
            "api_key": "sk-embed-1234567890",
            "base_url": "https://embed.example.com/v1",
        },
    )
    assert response.status_code == 200
    body = client.get("/api/v1/settings/embedding").json()
    assert body["engine"] == "api" and body["provider"] == "openai"
    assert body["credential_provider"] == "embedding:openai"
    assert body["base_url"] == "https://embed.example.com/v1"

    async def _check() -> None:
        from sqlalchemy import select

        from proseforge.infrastructure.database.models.remaining import (
            ProviderCredentialModel,
        )

        async with client.app.state.session_factory() as session:
            rows = list(await session.scalars(select(ProviderCredentialModel)))
            assert [row.provider for row in rows] == ["embedding:openai"]

    asyncio.run(_check())


def test_put_api_with_credential_name_uses_synthetic_name(client: TestClient):
    response = client.put(
        "/api/v1/settings/embedding",
        json={
            "engine": "api",
            "provider": "openai",
            "model": "embed-1",
            "api_key": "sk-embed-1234567890",
            "base_url": "https://embed.example.com/v1",
            "credential_name": "my-embed",
        },
    )
    assert response.status_code == 200
    assert response.json()["credential_provider"] == "embedding:my-embed"


def test_put_api_independent_credential_requires_base_url(client: TestClient):
    response = client.put(
        "/api/v1/settings/embedding",
        json={"engine": "api", "provider": "openai", "model": "embed-1", "api_key": "sk-embed-1234567890"},
    )
    assert response.status_code == 400
    # Nothing was saved: still the local default.
    assert client.get("/api/v1/settings/embedding").json()["engine"] == "local"


def test_put_api_resave_without_key_keeps_independent_credential(client: TestClient):
    """密钥不回显：再次保存时留空 api_key 表示沿用已存的独立凭证。"""
    first = client.put(
        "/api/v1/settings/embedding",
        json={
            "engine": "api",
            "provider": "openai",
            "model": "embed-1",
            "api_key": "sk-embed-1234567890",
            "base_url": "https://embed.example.com/v1",
        },
    )
    assert first.status_code == 200

    second = client.put(
        "/api/v1/settings/embedding",
        json={"engine": "api", "provider": "openai", "model": "embed-2"},
    )
    assert second.status_code == 200
    body = client.get("/api/v1/settings/embedding").json()
    assert body["model"] == "embed-2"
    assert body["credential_provider"] == "embedding:openai"
    assert body["base_url"] == "https://embed.example.com/v1"


def test_put_api_new_base_url_conflicts_with_existing_chunks(client: TestClient):
    """同 provider/model 但换独立 base：identity 带 host 而不同，触发既有 409。"""
    _seed_chunk(client, "openai/embed-1")
    payload = {
        "engine": "api",
        "provider": "openai",
        "model": "embed-1",
        "api_key": "sk-embed-1234567890",
        "base_url": "https://embed.example.com/v1",
    }
    response = client.put("/api/v1/settings/embedding", json=payload)
    assert response.status_code == 409

    forced = client.put("/api/v1/settings/embedding", json={**payload, "force": True})
    assert forced.status_code == 200
    assert forced.json()["credential_provider"] == "embedding:openai"


def test_resolve_api_client_prefers_independent_credential(client: TestClient):
    """credential_provider 命中独立凭证；缺省回退到 provider 同名聊天凭证。"""
    from proseforge.application.retrieval.indexing import _resolve_api_client

    response = client.post(
        "/api/v1/credentials",
        json={"provider": "openai", "api_key": "sk-chat-1234567890", "base_url": "https://chat.example.com/v1"},
    )
    assert response.status_code == 201
    response = client.put(
        "/api/v1/settings/embedding",
        json={
            "engine": "api",
            "provider": "openai",
            "model": "embed-1",
            "api_key": "sk-embed-1234567890",
            "base_url": "https://embed.example.com/v1",
        },
    )
    assert response.status_code == 200

    async def _resolve() -> None:
        from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork

        user_id = await _owner_id(client)
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            dedicated = await _resolve_api_client(
                uow, user_id, MASTER_KEY,
                provider="openai", model="embed-1",
                credential_provider="embedding:openai", base_url="https://embed.example.com/v1",
            )
            assert dedicated is not None
            assert dedicated.api_key == "sk-embed-1234567890"
            assert dedicated.base_url == "https://embed.example.com/v1"
            fallback = await _resolve_api_client(uow, user_id, MASTER_KEY, provider="openai", model="embed-1")
            assert fallback is not None
            assert fallback.api_key == "sk-chat-1234567890"
            assert fallback.base_url == "https://chat.example.com/v1"

    asyncio.run(_resolve())
