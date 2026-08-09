"""DELETE /api/v1/credentials/{id} — owner-scoped credential deletion.

Runs the real app on native sqlite (TestClient + lifespan): setup/login,
create a credential through POST, then exercise the delete endpoint.
Nothing references credentials by id (all lookups are by
(user_id, provider)), so deletion is unconditional — there is no 409 case;
missing and foreign ids share a uniform 404.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.infrastructure.database.models.remaining import ProviderCredentialModel
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


def _create_credential(client: TestClient) -> str:
    response = client.post("/api/v1/credentials", json={"provider": "openai", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201
    return response.json()["id"]


def test_delete_own_credential(client: TestClient):
    credential_id = _create_credential(client)

    assert client.delete(f"/api/v1/credentials/{credential_id}").status_code == 204
    assert client.get("/api/v1/credentials").json() == []
    # Second delete: already gone -> uniform 404.
    assert client.delete(f"/api/v1/credentials/{credential_id}").status_code == 404


def test_delete_missing_credential_is_404(client: TestClient):
    assert client.delete("/api/v1/credentials/does-not-exist").status_code == 404


def test_delete_foreign_credential_is_404(client: TestClient):
    # Seed another user's credential straight into the database (the product
    # is single-account, so there is no API path to create one).
    async def _seed() -> str:
        async with client.app.state.session_factory() as session:
            row = ProviderCredentialModel(id="cred-foreign", user_id="user-2", provider="openai", encrypted_payload="e")
            session.add(row)
            await session.commit()
            return row.id

    import asyncio

    foreign_id = asyncio.run(_seed())
    response = client.delete(f"/api/v1/credentials/{foreign_id}")
    assert response.status_code == 404
    # The foreign row must still be there.
    async def _count() -> int:
        async with client.app.state.session_factory() as session:
            return await session.get(ProviderCredentialModel, foreign_id) is not None

    assert asyncio.run(_count()) is True


def test_delete_credential_requires_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.delete("/api/v1/credentials/whatever").status_code == 401


def _seed_catalog(client: TestClient, provider: str, model_id: str, *, manual: bool = False) -> None:
    import asyncio

    from proseforge.domain.ports.model_provider import ProviderModel
    from proseforge.infrastructure.database.repositories.model_catalog import (
        SqlAlchemyModelCatalogRepository,
    )

    capabilities = {"availability": "available"}
    if manual:
        capabilities["manual"] = True

    async def _run() -> None:
        async with client.app.state.session_factory() as session:
            repo = SqlAlchemyModelCatalogRepository(session)
            await repo.upsert([ProviderModel(provider, model_id, model_id, capabilities)])
            await session.commit()

    asyncio.run(_run())


def _model_ids(client: TestClient) -> set[str]:
    return {row["model_id"] for row in client.get("/api/v1/models").json()}


def test_delete_last_credential_hides_synced_models(client: TestClient):
    _seed_catalog(client, "openai", "gpt-test-synced")
    # Owned manual row (post-0050 semantics: user-managed, owner-visible).
    response = client.post("/api/v1/models", json={"provider": "openai", "model_id": "gpt-test-manual"})
    assert response.status_code == 201
    credential_id = _create_credential(client)
    assert "gpt-test-synced" in _model_ids(client)

    assert client.delete(f"/api/v1/credentials/{credential_id}").status_code == 204

    remaining = _model_ids(client)
    # Synced models leave the picker with the last credential; owned manual
    # entries are user-managed and stay.
    assert "gpt-test-synced" not in remaining
    assert "gpt-test-manual" in remaining


def test_delete_credential_hides_models_even_when_other_user_remains(client: TestClient):
    import asyncio

    _seed_catalog(client, "deepseek", "deepseek-test-model")
    credential_id = client.post(
        "/api/v1/credentials", json={"provider": "deepseek", "api_key": "sk-test-abcdefghij"}
    ).json()["id"]

    async def _seed_foreign() -> None:
        async with client.app.state.session_factory() as session:
            session.add(ProviderCredentialModel(id="cred-foreign-2", user_id="user-2", provider="deepseek", encrypted_payload="e"))
            await session.commit()

    asyncio.run(_seed_foreign())

    assert client.delete(f"/api/v1/credentials/{credential_id}").status_code == 204
    # Listing is scoped to the caller's own credentials: another user's
    # credential must not keep the provider's models visible here.
    assert "deepseek-test-model" not in _model_ids(client)


def test_models_endpoint_hides_synced_models_without_credential(client: TestClient):
    # Read-path invariant (long-term): synced catalog rows whose provider
    # has no credential at all never reach the work/chat pickers — even if
    # they went stale before the delete-time cascade existed.
    _seed_catalog(client, "ghost-provider", "ghost-synced-model")
    # Owned manual row: user-managed placeholder, stays visible.
    response = client.post("/api/v1/models", json={"provider": "ghost-provider", "model_id": "ghost-manual-model"})
    assert response.status_code == 201
    # Legacy ownerless manual row (pre-0050): hidden like synced rows.
    _seed_catalog(client, "ghost-provider", "ghost-legacy-manual", manual=True)

    visible = _model_ids(client)
    assert "ghost-synced-model" not in visible
    assert "ghost-manual-model" in visible
    assert "ghost-legacy-manual" not in visible
