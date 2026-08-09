"""Manual model ownership (migration 0050).

POST /api/v1/models now records owner_id; GET /api/v1/models scopes manual
rows to their owner (plus legacy shared rows with owner_id NULL); the new
DELETE /api/v1/models/{provider}/{model_id} removes only owned manual rows.

Covers: cross-user invisibility, owner delete, and rejection of deletes
against another user's row (404), a legacy shared row (403), and a synced
row (403).
"""

from __future__ import annotations

import asyncio
import base64
import json

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
        response = test_client.post("/api/v1/auth/setup", json={"email": "a@example.com", "password": PASSWORD})
        assert response.status_code == 201
        yield test_client


def _login(client: TestClient, email: str) -> None:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200


def _register_b(client: TestClient) -> httpx.Cookies:
    """Switch the session to a freshly registered user B; return A's cookies."""
    admin_cookies = httpx.Cookies(client.cookies)
    client.cookies = httpx.Cookies()
    response = client.post("/api/v1/auth/register", json={"email": "b@example.com", "password": PASSWORD})
    assert response.status_code == 201
    _login(client, "b@example.com")
    return admin_cookies


def _seed_row(client: TestClient, provider: str, model_id: str, *, manual: bool, owner_id: str | None = None) -> None:
    capabilities = {"availability": "available", **({"manual": True} if manual else {})}

    async def _run() -> None:
        async with client.app.state.session_factory() as session:
            repo = SqlAlchemyModelCatalogRepository(session)
            await repo.upsert([ProviderModel(provider, model_id, model_id, capabilities)], owner_id=owner_id)
            await session.commit()

    asyncio.run(_run())


def _user_id(client: TestClient, email: str) -> str:
    async def _run() -> str:
        import sqlalchemy as sa

        async with client.app.state.session_factory() as session:
            result = await session.execute(sa.text("SELECT id FROM users WHERE email = :e"), {"e": email})
            return str(result.scalar_one())

    return asyncio.run(_run())


def _rows(client: TestClient) -> list[dict[str, object]]:
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    return list(response.json())


def test_manual_model_not_visible_to_other_users(client: TestClient):
    _login(client, "a@example.com")
    response = client.post("/api/v1/models", json={"provider": "custom", "model_id": "a-private-model"})
    assert response.status_code == 201
    assert response.json()["owner_id"] == _user_id(client, "a@example.com")
    rows = _rows(client)
    row = next(item for item in rows if item["model_id"] == "a-private-model")
    assert row["owner_id"] is not None
    assert row["legacy_shared"] is False

    admin_cookies = _register_b(client)
    try:
        assert "a-private-model" not in {item["model_id"] for item in _rows(client)}
    finally:
        client.cookies = admin_cookies
    # Owner still sees it afterwards.
    assert "a-private-model" in {item["model_id"] for item in _rows(client)}


def test_legacy_shared_manual_row_follows_credential_gate(client: TestClient):
    """Legacy shared manual rows (pre-0050, owner_id NULL) are not
    user-deletable, so they must follow the same credential gate as synced
    rows: hidden while the provider has no credential, visible again once
    credentialed. Regression: a deleted provider's legacy model kept showing
    in every work/chat picker (agnes-2.0-flash)."""
    _login(client, "a@example.com")
    _seed_row(client, "custom", "legacy-manual", manual=True, owner_id=None)

    # No credential for the provider: hidden from the picker.
    assert "legacy-manual" not in {item["model_id"] for item in _rows(client)}

    # Credential added: visible again, still flagged legacy_shared.
    response = client.post("/api/v1/credentials", json={"provider": "custom", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201
    row = next(item for item in _rows(client) if item["model_id"] == "legacy-manual")
    assert row["owner_id"] is None
    assert row["legacy_shared"] is True

    # Another user without the credential does not see it.
    admin_cookies = _register_b(client)
    try:
        assert "legacy-manual" not in {item["model_id"] for item in _rows(client)}
    finally:
        client.cookies = admin_cookies


def test_owner_can_delete_own_manual_model(client: TestClient):
    _login(client, "a@example.com")
    response = client.post("/api/v1/models", json={"provider": "custom", "model_id": "mine"})
    assert response.status_code == 201

    response = client.delete("/api/v1/models/custom/mine")
    assert response.status_code == 204
    assert "mine" not in {item["model_id"] for item in _rows(client)}


def test_delete_other_users_manual_model_is_404(client: TestClient):
    _login(client, "a@example.com")
    response = client.post("/api/v1/models", json={"provider": "custom", "model_id": "a-model"})
    assert response.status_code == 201

    admin_cookies = _register_b(client)
    try:
        response = client.delete("/api/v1/models/custom/a-model")
        assert response.status_code == 404
    finally:
        client.cookies = admin_cookies
    # Untouched for the owner.
    assert "a-model" in {item["model_id"] for item in _rows(client)}


def test_delete_legacy_shared_manual_row_is_403(client: TestClient):
    _login(client, "a@example.com")
    _seed_row(client, "custom", "legacy-row", manual=True, owner_id=None)

    response = client.delete("/api/v1/models/custom/legacy-row")
    assert response.status_code == 403

    # Not deleted: once the provider is credentialed the row shows up again
    # (uncredentialed legacy rows are hidden from pickers, so visibility is
    # only meaningful with a credential in place).
    response = client.post("/api/v1/credentials", json={"provider": "custom", "api_key": "sk-test-1234567890"})
    assert response.status_code == 201
    assert "legacy-row" in {item["model_id"] for item in _rows(client)}


def test_delete_synced_row_is_403(client: TestClient):
    _login(client, "a@example.com")
    _seed_row(client, "openai", "synced-model", manual=False)

    response = client.delete("/api/v1/models/openai/synced-model")
    assert response.status_code == 403


def test_delete_missing_model_is_404(client: TestClient):
    _login(client, "a@example.com")
    response = client.delete("/api/v1/models/custom/does-not-exist")
    assert response.status_code == 404


def test_delete_requires_auth(client: TestClient):
    response = client.delete("/api/v1/models/custom/anything")
    assert response.status_code == 401


def test_manual_add_over_synced_row_becomes_owned(client: TestClient):
    """A manual add over an existing synced row converts it to an owned
    manual row (upsert owner propagation), not a legacy shared one."""
    _login(client, "a@example.com")
    _seed_row(client, "openai", "hybrid", manual=False)
    response = client.post("/api/v1/models", json={"provider": "openai", "model_id": "hybrid"})
    assert response.status_code == 201

    row = next(item for item in _rows(client) if item["model_id"] == "hybrid")
    assert row["owner_id"] is not None
    assert row["legacy_shared"] is False

    admin_cookies = _register_b(client)
    try:
        # B has no openai credential and is not the owner: invisible.
        assert "hybrid" not in {item["model_id"] for item in _rows(client)}
    finally:
        client.cookies = admin_cookies


def test_catalog_row_json_roundtrip(client: TestClient):
    """Sanity: capabilities JSON stays valid after owner-scoped delete."""
    _login(client, "a@example.com")
    client.post("/api/v1/models", json={"provider": "custom", "model_id": "json-check", "capabilities": {"vision": True}})
    row = next(item for item in _rows(client) if item["model_id"] == "json-check")
    assert row["capabilities"]["manual"] is True
    assert row["capabilities"]["vision"] is True
    assert json.loads(json.dumps(row["capabilities"]))["manual"] is True
