from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from proseforge.api.main import create_app
from proseforge.application.agents.policy_snapshot import verify
from proseforge.settings import Settings


def _database_url() -> str:
    url = os.environ.get("PROSEFORGE_TEST_DATABASE_URL")
    assert url, "PROSEFORGE_TEST_DATABASE_URL is required (API tests run in the B1 batch)"
    return url


def _run_sql(statement: str, **params):
    """用一次性引擎直改/直读数据库（独立事件循环，避免与 TestClient portal 争用连接）。"""

    async def _execute():
        engine = create_async_engine(_database_url())
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), params)
                return result.mappings().all() if result.returns_rows else None
        finally:
            await engine.dispose()

    return asyncio.run(_execute())


def _force_run_status(run_id: str, status: str) -> None:
    _run_sql("UPDATE agent_runs SET status = :status WHERE id = :id", status=status, id=run_id)


def _cancel_active_runs() -> None:
    # 并发额度按 PENDING/RUNNING 计数；每个用例开局清场，保证计数确定性。
    _run_sql("UPDATE agent_runs SET status = 'CANCELLED' WHERE status IN ('PENDING', 'RUNNING', 'PAUSED')")


def _create_project(auth_client) -> dict:
    response = auth_client.post_json("/api/v1/projects", {"slug": f"sec-{uuid.uuid4().hex[:12]}", "title": "Agent Security"})
    assert response.status_code == 201, response.text
    return response.json()


def _create_run(auth_client, project_id: str, goal: str = "策略安全用例", idempotency_key: str | None = None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    # agent_runs 路由只挂在 /api/v3（main.py 无 /api/v1 别名），v1 路径会 404。
    return auth_client.post(f"/api/v3/projects/{project_id}/agent-runs", json={"goal": goal}, headers=headers)


def test_run_creation_writes_signed_policy_snapshot(auth_client, api_settings):
    _cancel_active_runs()
    project = _create_project(auth_client)
    response = _create_run(auth_client, project["id"], "签署策略快照")
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["policy_version"] == "v3-policy-1"

    rows = _run_sql("SELECT policy_version, payload, signature FROM agent_policy_snapshots WHERE run_id = :id", id=run["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["policy_version"] == "v3-policy-1"
    assert row["signature"]
    assert verify(json.loads(row["payload"]), row["signature"], api_settings.master_key)
    _force_run_status(run["id"], "CANCELLED")


def test_failed_run_can_only_be_retried_not_resumed(auth_client):
    _cancel_active_runs()
    project = _create_project(auth_client)
    run = _create_run(auth_client, project["id"], "失败恢复语义").json()
    _force_run_status(run["id"], "FAILED")

    resume = auth_client.post(f"/api/v3/agent-runs/{run['id']}/resume")
    assert resume.status_code == 409
    assert resume.json()["error"]["code"] == "INVALID_RUN_TRANSITION"

    retry = auth_client.post(f"/api/v3/agent-runs/{run['id']}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "RUNNING"

    events = auth_client.get(f"/api/v3/agent-runs/{run['id']}/events").json()["events"]
    retry_event = next(item for item in events if item["event"] == "run.retry")
    data = retry_event["data"]
    assert data["actor"]
    assert data["action"] == "retry"
    assert data["decision"] == "allow"
    assert data["policy_version"] == "v3-policy-1"
    assert data["resource_id"] == run["id"]
    assert "goal" not in data and "prompt" not in data

    assert auth_client.post(f"/api/v3/agent-runs/{run['id']}/cancel").status_code == 200


def test_control_rejects_tampered_policy_snapshot(auth_client):
    _cancel_active_runs()
    project = _create_project(auth_client)
    run = _create_run(auth_client, project["id"], "篡改策略快照").json()
    _run_sql("UPDATE agent_policy_snapshots SET signature = :signature WHERE run_id = :id", signature="0" * 64, id=run["id"])

    pause = auth_client.post(f"/api/v3/agent-runs/{run['id']}/pause")
    assert pause.status_code == 409
    assert pause.json()["error"]["code"] == "POLICY_VIOLATION"

    audit = auth_client.get(f"/api/v3/agent-runs/{run['id']}/audit").json()
    violations = [row for row in audit if row["event"] == "run.policy_violation"]
    assert violations
    payload = violations[-1]["payload"]
    assert payload["decision"] == "deny"
    assert payload["action"] == "pause"
    assert payload["policy_version"] == "v3-policy-1"
    # 篡改后控制面 fail-closed，直接改库释放并发额度。
    _force_run_status(run["id"], "CANCELLED")


def test_run_concurrency_limit(auth_client):
    _cancel_active_runs()
    project = _create_project(auth_client)
    runs = []
    for index in range(3):
        response = _create_run(auth_client, project["id"], f"并发额度 {index}")
        assert response.status_code == 201, response.text
        runs.append(response.json()["id"])

    fourth = _create_run(auth_client, project["id"], "并发额度溢出")
    assert fourth.status_code == 409
    assert fourth.json()["error"]["code"] == "RUN_CONCURRENCY_LIMIT"
    for run_id in runs:
        _force_run_status(run_id, "CANCELLED")


def test_idempotency_key_replays_same_run(auth_client):
    _cancel_active_runs()
    project = _create_project(auth_client)
    key = f"idem-{uuid.uuid4().hex[:12]}"
    first = _create_run(auth_client, project["id"], "幂等建跑", key)
    second = _create_run(auth_client, project["id"], "幂等建跑", key)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    other = _create_run(auth_client, project["id"], "幂等建跑", f"idem-{uuid.uuid4().hex[:12]}")
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]
    _force_run_status(first.json()["id"], "CANCELLED")
    _force_run_status(other.json()["id"], "CANCELLED")


def test_agent_rate_limit_returns_429_envelope_for_excess_writes():
    client = TestClient(create_app())
    for _ in range(20):
        assert client.post("/api/v3/agent-runs/run-x/pause").status_code == 401
    limited = client.post("/api/v3/agent-runs/run-x/pause")
    assert limited.status_code == 429
    body = limited.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["retryable"] is True
    assert limited.headers["Retry-After"]


def test_agent_rate_limit_separates_read_and_write_buckets():
    client = TestClient(create_app())
    for _ in range(60):
        assert client.get("/api/v3/agent-runs/run-x").status_code == 401
    assert client.get("/api/v3/agent-runs/run-x").status_code == 429
    # 读桶耗尽不影响写桶。
    assert client.post("/api/v3/agent-runs/run-x/pause").status_code == 401


def test_agent_rate_limit_does_not_touch_v1_or_v2():
    client = TestClient(create_app())
    for _ in range(70):
        assert client.get("/api/v1/health/live").status_code == 200


AUTH_ORIGIN = "http://testserver"


def _auth_test_client(tmp_path, *, allow_registration: bool = True) -> TestClient:
    """SQLite-backed app so /api/v1/auth/* exercises the real login/register paths."""
    settings = Settings(
        runtime_profile="native",
        public_url=AUTH_ORIGIN,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=base64.b64encode(b"k" * 32).decode(),
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=allow_registration,
    )
    return TestClient(create_app(settings))


def _post_auth(client: TestClient, path: str, payload: dict):
    return client.post(path, json=payload, headers={"Origin": AUTH_ORIGIN})


def test_auth_rate_limit_returns_429_after_failed_logins(tmp_path):
    with _auth_test_client(tmp_path) as client:
        payload = {"email": "nobody@example.com", "password": "p" * 12}
        for _ in range(10):
            assert _post_auth(client, "/api/v1/auth/login", payload).status_code == 401
        limited = _post_auth(client, "/api/v1/auth/login", payload)
        assert limited.status_code == 429
        body = limited.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        assert body["error"]["retryable"] is True
        assert limited.headers["Retry-After"]


def test_auth_rate_limit_counts_register_flood(tmp_path):
    with _auth_test_client(tmp_path) as client:
        for index in range(10):
            payload = {"email": f"user{index}@example.com", "password": "p" * 12}
            assert _post_auth(client, "/api/v1/auth/register", payload).status_code == 201
        limited = _post_auth(client, "/api/v1/auth/register", {"email": "overflow@example.com", "password": "p" * 12})
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_auth_rate_limit_ignores_successful_logins_and_setup_probes(tmp_path):
    with _auth_test_client(tmp_path) as client:
        credentials = {"email": "owner@example.com", "password": "p" * 12}
        assert _post_auth(client, "/api/v1/auth/setup", credentials).status_code == 201
        # 成功登录与 setup 稳态 409（幂等探测）不计入配额，远超阈值也不触发 429。
        for _ in range(15):
            assert _post_auth(client, "/api/v1/auth/login", credentials).status_code == 200
            assert _post_auth(client, "/api/v1/auth/setup", {"email": "other@example.com", "password": "p" * 12}).status_code == 409
