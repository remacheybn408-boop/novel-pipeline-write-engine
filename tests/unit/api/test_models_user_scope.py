"""GET /api/v1/models — per-user credential scoping (regression).

User A configures a provider credential; user B's model list must not
surface that provider's synced models, even though the catalog rows are
shared. Listing is filtered by the caller's own credentials only.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.domain.ports.model_provider import ProviderModel
from proseforge.infrastructure.database.repositories.model_catalog import (
    SqlAlchemyModelCatalogRepository,
)
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
PASSWORD = "p" * 12


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=True,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _login(client: TestClient, email: str) -> None:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def _seed_catalog(client: TestClient, provider: str, model_id: str) -> None:
    async def _run() -> None:
        async with client.app.state.session_factory() as session:
            repo = SqlAlchemyModelCatalogRepository(session)
            await repo.upsert([ProviderModel(provider, model_id, model_id, {"availability": "available"})])
            await session.commit()

    asyncio.run(_run())


def _model_ids(client: TestClient) -> set[str]:
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    return {row["model_id"] for row in response.json()}


def test_models_not_leaked_across_users(client: TestClient):
    # User A (setup admin) configures a credential and syncs catalog rows.
    response = client.post("/api/v1/auth/setup", json={"email": "a@example.com", "password": PASSWORD})
    assert response.status_code == 201
    _login(client, "a@example.com")
    response = client.post("/api/v1/credentials", json={"provider": "openai", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201
    _seed_catalog(client, "openai", "gpt-leak-check")
    assert "gpt-leak-check" in _model_ids(client)

    admin_cookies = httpx.Cookies(client.cookies)
    client.cookies = httpx.Cookies()
    try:
        # User B (plain USER) has no credentials at all.
        response = client.post("/api/v1/auth/register", json={"email": "b@example.com", "password": PASSWORD})
        assert response.status_code == 201
        _login(client, "b@example.com")
        assert "gpt-leak-check" not in _model_ids(client)
    finally:
        client.cookies = admin_cookies

    # Sanity: A still sees the model afterwards.
    assert "gpt-leak-check" in _model_ids(client)
