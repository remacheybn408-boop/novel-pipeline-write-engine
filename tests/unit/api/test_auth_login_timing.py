"""Login email-enumeration / timing side-channel regression tests.

An unknown email used to short-circuit before argon2 verification, making
account existence distinguishable through response timing. Both failure
paths must now run ``verify_password`` (against a dummy hash when the user
does not exist) and return an identical 401 response.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
JWT_SECRET = "j" * 32
ORIGIN = "http://testserver"


def _make_client(tmp_path) -> TestClient:
    settings = Settings(
        runtime_profile="native",
        environment="development",
        public_url=ORIGIN,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=True,
    )
    return TestClient(create_app(settings))


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers={"Origin": ORIGIN})


def test_unknown_email_and_wrong_password_return_identical_response(tmp_path):
    with _make_client(tmp_path) as client:
        client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        unknown = _login(client, "ghost@example.com", "p" * 12)
        wrong_password = _login(client, "guest@example.com", "q" * 12)
        assert unknown.status_code == 401
        assert wrong_password.status_code == 401
        assert unknown.json() == wrong_password.json()


def test_unknown_email_still_runs_password_verification(tmp_path, monkeypatch):
    with _make_client(tmp_path) as client:
        client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": ORIGIN})
        calls: list[tuple[str, str]] = []
        real_verify = client.app.state.auth.verify_password

        def spy(password: str, password_hash: str) -> bool:
            calls.append((password, password_hash))
            return real_verify(password, password_hash)

        monkeypatch.setattr(client.app.state.auth, "verify_password", spy)
        assert _login(client, "ghost@example.com", "p" * 12).status_code == 401
        assert _login(client, "guest@example.com", "q" * 12).status_code == 401
        # Both failure paths must pay the argon2 verification cost.
        assert len(calls) == 2
        assert all(password_hash for _, password_hash in calls)
