"""Login cookie Secure flag regression tests (RFC 6265).

The Secure flag must follow the scheme the app is actually served over
(settings.public_url), never the environment name: a production deployment
behind plain HTTP (e.g. a bare-IP install) must not emit Secure cookies, or
browsers refuse to store them and the session dies immediately.
"""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
JWT_SECRET = "j" * 32


def _make_client(tmp_path, *, public_url: str, environment: str = "development") -> TestClient:
    settings = Settings(
        runtime_profile="native",
        environment=environment,
        public_url=public_url,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=True,
    )
    return TestClient(create_app(settings))


def _login_set_cookie(client: TestClient, origin: str) -> str:
    client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": origin})
    response = client.post("/api/v1/auth/login", json={"email": "guest@example.com", "password": "p" * 12}, headers={"Origin": origin})
    assert response.status_code == 200, response.text
    return response.headers["set-cookie"]


def test_http_public_url_cookie_is_not_secure(tmp_path):
    origin = "http://testserver"
    with _make_client(tmp_path, public_url=origin) as client:
        assert "secure" not in _login_set_cookie(client, origin).lower()


def test_http_public_url_production_cookie_is_not_secure(tmp_path):
    origin = "http://testserver"
    with _make_client(tmp_path, public_url=origin, environment="production") as client:
        assert "secure" not in _login_set_cookie(client, origin).lower()


def test_https_public_url_cookie_is_secure(tmp_path):
    origin = "https://testserver"
    with _make_client(tmp_path, public_url=origin) as client:
        assert "secure" in _login_set_cookie(client, origin).lower()
