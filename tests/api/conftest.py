from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://testserver"
SETUP_EMAIL = "t@example.local"
SETUP_PASSWORD = "twelve-char-pw"


@pytest.fixture(scope="session")
def api_settings(tmp_path_factory):
    database_url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("PROSEFORGE_TEST_DATABASE_URL is required (API tests run in the B1 batch)")
    from proseforge.settings import Settings

    return Settings(
        database_url=database_url,
        public_url=ORIGIN,
        blob_root=str(tmp_path_factory.mktemp("blobs")),
        backup_root=str(tmp_path_factory.mktemp("backups")),
        data_dir=str(tmp_path_factory.mktemp("data")),
        runtime_profile="test",
    )


@pytest.fixture(scope="session")
def client(api_settings):
    sync_url = os.environ.get("PROSEFORGE_SYNC_DATABASE_URL")
    if not sync_url:
        pytest.skip("PROSEFORGE_SYNC_DATABASE_URL is required (API tests run in the B1 batch)")
    from alembic import command
    from alembic.config import Config

    from proseforge.api.main import create_app

    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "proseforge" / "infrastructure" / "database" / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(alembic_cfg, "head")  # 顺便验证迁移链
    # 必须走上下文管理器：裸 TestClient 每个请求新开 anyio portal（独立事件循环），
    # 跨请求复用的 asyncpg 连接会报 "attached to a different loop"。
    # with 块内整个会话共享一个 portal，并运行 lifespan（启动真实生命周期）。
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


def _insert_user_row(user_id: str, email: str, password_hash: str, role: str) -> None:
    """Insert one users row via a one-shot engine on its own loop (same pattern
    as _run_sql in the test modules — never shares connections with the
    TestClient portal)."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _insert():
        engine = create_async_engine(os.environ["PROSEFORGE_TEST_DATABASE_URL"])
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO users (id, email, password_hash, role, session_version) VALUES (:id, :email, :password_hash, :role, 1)"),
                    {"id": user_id, "email": email, "password_hash": password_hash, "role": role},
                )
        finally:
            await engine.dispose()

    asyncio.run(_insert())


def _create_user_headers(api_settings, user_id: str, role: str = "ADMIN") -> dict:
    """Insert a real users row and return bearer headers for it.

    current_user resolves the token's session_version against the users
    table, so a self-signed JWT alone no longer authenticates — the
    principal must exist with a matching session_version (default 1).
    """
    from proseforge.application.auth.service import AuthService, AuthUser

    email = f"{user_id}@example.local"
    _insert_user_row(user_id, email, "test-only-placeholder", role)
    token = AuthService(api_settings.jwt_secret.get_secret_value()).issue_token(AuthUser(user_id, email, role))
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


@pytest.fixture()
def user_headers_factory(api_settings):
    """Factory fixture: (user_id, role="ADMIN") -> bearer headers for a real users row."""

    def _make(user_id: str, role: str = "ADMIN") -> dict:
        return _create_user_headers(api_settings, user_id, role)

    return _make


class AuthClient:
    """TestClient wrapper that injects the Origin header require_same_origin expects."""

    def __init__(self, client: TestClient):
        self._client = client

    @property
    def raw(self) -> TestClient:
        return self._client

    def get(self, url: str, **kwargs):
        return self._client.get(url, **kwargs)

    def stream(self, method: str, url: str, **kwargs):
        return self._client.stream(method, url, **kwargs)

    def post_json(self, url: str, payload: dict):
        return self._client.post(url, json=payload, headers={"Origin": ORIGIN})

    def post(self, url: str, **kwargs):
        headers = {"Origin": ORIGIN, **kwargs.pop("headers", {})}
        return self._client.post(url, headers=headers, **kwargs)


@pytest.fixture()
def auth_client(client, api_settings):
    response = client.post("/api/v1/auth/setup", json={"email": SETUP_EMAIL, "password": SETUP_PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code in (201, 409)  # 会话内只首个用例 201
    # setup 不签发会话 cookie，必须显式登录
    login = client.post("/api/v1/auth/login", json={"email": SETUP_EMAIL, "password": SETUP_PASSWORD}, headers={"Origin": ORIGIN})
    if login.status_code == 401:
        # user_headers_factory 直插的用户会让一次性 setup 端点提前关闭（409），
        # 但 SETUP_EMAIL 本人可能从未建过——补建行（真实口令散列）后重试登录。
        from proseforge.application.auth.service import AuthService

        password_hash = AuthService(api_settings.jwt_secret.get_secret_value()).hash_password(SETUP_PASSWORD)
        _insert_user_row("setup-owner", SETUP_EMAIL, password_hash, "ADMIN")
        login = client.post("/api/v1/auth/login", json={"email": SETUP_EMAIL, "password": SETUP_PASSWORD}, headers={"Origin": ORIGIN})
    assert login.status_code == 200
    return AuthClient(client)
