"""Session revocation semantics for multi-account coexistence.

Logout must NOT bump session_version: with per-tab bearer tokens several
tabs/devices share one account, so one tab signing out must not kick the
others. Password change remains the revocation path and still bumps it.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
ORIGIN = "http://testserver"
EMAIL = "owner@example.com"
PASSWORD = "p" * 12
NEW_PASSWORD = "q" * 12


def _make_client(tmp_path) -> TestClient:
    settings = Settings(
        runtime_profile="native",
        public_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    return TestClient(create_app(settings))


def _login_token(client: TestClient, password: str = PASSWORD) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": EMAIL, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _setup_owner(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/setup",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 201, response.text


def test_logout_does_not_revoke_the_token(tmp_path):
    with _make_client(tmp_path) as client:
        _setup_owner(client)
        token = _login_token(client)
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 200

        logout = client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN, **_bearer(token)})
        assert logout.status_code == 204

        # The same bearer token still resolves: logout no longer bumps
        # session_version, so coexisting tabs/devices keep their sessions.
        me = client.get("/api/v1/auth/me", headers=_bearer(token))
        assert me.status_code == 200 and me.json()["email"] == EMAIL


def test_password_change_still_revokes_old_tokens(tmp_path):
    with _make_client(tmp_path) as client:
        _setup_owner(client)
        token = _login_token(client)

        changed = client.put(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers={"Origin": ORIGIN, **_bearer(token)},
        )
        assert changed.status_code == 204, changed.text

        # The password change bumped session_version: the old token is dead.
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401

        # Re-login with the new password issues a working token.
        new_token = _login_token(client, password=NEW_PASSWORD)
        assert client.get("/api/v1/auth/me", headers=_bearer(new_token)).status_code == 200
