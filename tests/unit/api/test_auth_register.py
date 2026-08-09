"""POST /api/v1/auth/register — self-service USER registration.

Gated by settings.allow_registration (PROSEFORGE_ALLOW_REGISTRATION): a
personal instance stays single-owner (403), a shared/demo instance lets
visitors create plain USER accounts. Registration never mints an ADMIN —
that role is reserved for the one-time /auth/setup owner.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
ORIGIN = "http://testserver"


def _make_client(tmp_path, *, allow_registration: bool, raise_server_exceptions: bool = True) -> TestClient:
    settings = Settings(
        runtime_profile="native",
        public_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=allow_registration,
    )
    return TestClient(create_app(settings), raise_server_exceptions=raise_server_exceptions)


def test_register_disabled_by_default(tmp_path):
    with _make_client(tmp_path, allow_registration=False) as client:
        response = client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert response.status_code == 403


def test_register_creates_plain_user_and_login_works(tmp_path):
    with _make_client(tmp_path, allow_registration=True) as client:
        response = client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "USER"
        assert response.json()["email"] == "guest@example.com"

        # Duplicate email -> 409.
        duplicate = client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert duplicate.status_code == 409

        # The new account signs in and resolves /me with the USER role.
        login = client.post("/api/v1/auth/login", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert login.status_code == 200, login.text
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200 and me.json()["role"] == "USER"


def test_register_coexists_with_setup_owner(tmp_path):
    with _make_client(tmp_path, allow_registration=True) as client:
        setup = client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert setup.status_code == 201 and setup.json()["role"] == "ADMIN"
        # Setup stays one-time even with open registration.
        again = client.post("/api/v1/auth/setup", json={"email": "second@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert again.status_code == 409
        # Registration keeps working alongside the owner.
        guest = client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        assert guest.status_code == 201 and guest.json()["role"] == "USER"


def test_register_rejects_malformed_email(tmp_path):
    with _make_client(tmp_path, allow_registration=True) as client:
        for bad_email in ("not-an-email", "missing@domain", "spaces in@addr.com", "@nodomain.com"):
            response = client.post(
                "/api/v1/auth/register", json={"email": bad_email, "password": "p" * 12}, headers={"Origin": ORIGIN}
            )
            assert response.status_code == 422, bad_email


def test_setup_rejects_malformed_email(tmp_path):
    with _make_client(tmp_path, allow_registration=False) as client:
        response = client.post(
            "/api/v1/auth/setup", json={"email": "not-an-email", "password": "p" * 12}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 422


def test_registration_status_is_public(tmp_path):
    # No session needed; the login page queries it before any login.
    with _make_client(tmp_path, allow_registration=False) as client:
        response = client.get("/api/v1/auth/registration-status")
        assert response.status_code == 200 and response.json() == {"enabled": False, "initialized": False}
    with _make_client(tmp_path, allow_registration=True) as client:
        response = client.get("/api/v1/auth/registration-status")
        assert response.status_code == 200 and response.json() == {"enabled": True, "initialized": False}
        # Once any account exists the login page switches from setup to register.
        created = client.post("/api/v1/auth/register", json={"email": "first@example.com", "password": "twelve-char-pw"}, headers={"Origin": "http://testserver"})
        assert created.status_code == 201, created.text
        response = client.get("/api/v1/auth/registration-status")
        assert response.status_code == 200 and response.json() == {"enabled": True, "initialized": True}


def test_register_db_failure_is_500_not_409(tmp_path, monkeypatch):
    """A non-IntegrityError database failure must not be misreported as a
    duplicate-email conflict."""
    from proseforge.infrastructure.database.repositories.user import (
        SqlAlchemyUserRepository,
    )

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(SqlAlchemyUserRepository, "create", _boom)
    with _make_client(tmp_path, allow_registration=True, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 500


def test_setup_db_failure_is_500_not_409(tmp_path, monkeypatch):
    from proseforge.infrastructure.database.repositories.user import (
        SqlAlchemyUserRepository,
    )

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(SqlAlchemyUserRepository, "create", _boom)
    with _make_client(tmp_path, allow_registration=False, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 500
