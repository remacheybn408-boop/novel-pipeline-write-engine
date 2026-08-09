"""POST /api/v3/projects/{id}/agent-runs cluster model selection.

With an effective cluster config (project override > global), start_run
ignores the request's provider/model — swarm models come from the cluster
config card; the run row keeps the resolved write-role model as a display
fallback. Effective cluster mode with < 2 available models -> 400. No
config anywhere -> the request values flow through (legacy behavior).
Real app on native sqlite (TestClient + lifespan).
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
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


def _seed_models(client: TestClient) -> None:
    response = client.post("/api/v1/credentials", json={"provider": "openai", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201

    async def _seed() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            await uow.model_catalog.upsert([
                ProviderModel("openai", "gpt-a", "GPT A", {}),
                ProviderModel("openai", "gpt-b", "GPT B", {}),
            ])
            await uow.commit()

    asyncio.run(_seed())


def _project(client: TestClient, slug: str = "novel-1") -> str:
    response = client.post("/api/v1/projects", json={"slug": slug, "title": "Novel", "mode": "work"})
    assert response.status_code == 201
    return response.json()["id"]


def _start_run(client: TestClient, project_id: str, **overrides):
    return client.post(
        f"/api/v3/projects/{project_id}/agent-runs",
        json={"goal": "写第三章", **overrides},
    )


def _run_provider_model(client: TestClient, run_id: str) -> tuple[str | None, str | None]:
    """Read the persisted run row: the response envelope does not expose
    provider/model, the display fallback lives on the row itself."""

    async def _read() -> tuple[str | None, str | None]:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            from proseforge.infrastructure.database.models.agents import AgentRunModel

            row = await uow.session.get(AgentRunModel, run_id)
            return row.provider, row.model

    return asyncio.run(_read())


def _put_global_cluster(client: TestClient, write_model: str) -> None:
    """Seed the global cluster preference row directly: the public build has
    no cluster settings API, but the swarm executor still resolves the stored
    config (user_preferences "cluster" key)."""
    user_id = client.get("/api/v1/auth/me").json()["id"]

    async def _seed() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            await uow.user_preferences.set(user_id, "cluster", json.dumps({"mode": "cluster", "write_model": write_model}))
            await uow.commit()

    asyncio.run(_seed())


def test_start_run_ignores_request_model_when_global_cluster_config(client: TestClient):
    _seed_models(client)
    _put_global_cluster(client, "openai/gpt-a")
    project_id = _project(client)

    response = _start_run(client, project_id, provider="deepseek", model="evil-model")

    assert response.status_code == 201, response.text
    # Request model ignored: display fallback is the configured write role.
    assert _run_provider_model(client, response.json()["id"]) == ("openai", "gpt-a")


def test_start_run_project_override_beats_global(client: TestClient):
    _seed_models(client)
    _put_global_cluster(client, "openai/gpt-a")
    project_id = _project(client)
    async def _seed_override() -> None:
        async with SqlAlchemyUnitOfWork(client.app.state.session_factory) as uow:
            from proseforge.infrastructure.database.models.project import ProjectModel

            row = await uow.session.get(ProjectModel, project_id)
            assert row is not None
            row.cluster_config_json = json.dumps({"mode": "cluster", "write_model": "openai/gpt-b"})
            await uow.commit()

    asyncio.run(_seed_override())

    response = _start_run(client, project_id, provider="deepseek", model="evil-model")

    assert response.status_code == 201, response.text
    assert _run_provider_model(client, response.json()["id"]) == ("openai", "gpt-b")


def test_start_run_400_when_effective_cluster_pool_shrinks(client: TestClient):
    _seed_models(client)
    _put_global_cluster(client, "openai/gpt-a")
    project_id = _project(client)

    # The pool can shrink AFTER the config was saved (credential removed):
    # effective cluster mode with < 2 available models must fail clearly.
    credentials = client.get("/api/v1/credentials").json()
    credential_id = credentials[0]["id"]
    assert client.delete(f"/api/v1/credentials/{credential_id}").status_code == 204

    response = _start_run(client, project_id)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "2 个模型" in error["message"]


def test_start_run_legacy_passthrough_without_any_config(client: TestClient):
    project_id = _project(client)

    response = _start_run(client, project_id, provider="deepseek", model="deepseek-chat")

    assert response.status_code == 201, response.text
    assert _run_provider_model(client, response.json()["id"]) == ("deepseek", "deepseek-chat")
