"""GET /api/v1/logs/errors/download: Markdown report of ERROR/CRITICAL
entries (with tracebacks), attachment headers, auth required."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from proseforge.api.main import create_app
from proseforge.settings import Settings

MASTER_KEY = base64.b64encode(b"k" * 32).decode()


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        runtime_profile="native",
        data_dir=str(tmp_path / "data"),
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
        test_client.log_dir = tmp_path / "data" / "logs"  # type: ignore[attr-defined]
        yield test_client


def _write_app_log(client: TestClient, lines: list[str]) -> None:
    # The lifespan bootstrap already ran setup_logging against this dir; the
    # open handler appends, so seed content by writing before the request.
    (client.log_dir / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")  # type: ignore[attr-defined]


def test_download_renders_errors_as_markdown(client: TestClient):
    _write_app_log(client, [
        "2026-07-30 10:00:00,000 INFO proseforge.api: startup ok",
        "2026-07-30 10:01:00,000 ERROR proseforge.worker: generation failed",
        "Traceback (most recent call last):",
        '  File "worker.py", line 1, in <module>',
        "ValueError: bad input",
        "2026-07-30 10:02:00,000 WARNING proseforge.api: slow request",
        "2026-07-30 10:03:00,000 CRITICAL proseforge.api: database down",
    ])
    response = client.get("/api/v1/logs/errors/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition and ".md" in disposition

    body = response.text
    assert body.startswith("# 错误日志报告")
    assert "错误条目数：2" in body
    # Error entries keep their traceback continuation lines.
    assert "generation failed" in body
    assert "ValueError: bad input" in body
    assert "database down" in body
    # INFO/WARNING records are filtered out.
    assert "startup ok" not in body
    assert "slow request" not in body


def test_download_without_errors_returns_explanatory_report(client: TestClient):
    _write_app_log(client, ["2026-07-30 10:00:00,000 INFO proseforge.api: all quiet"])
    response = client.get("/api/v1/logs/errors/download")
    assert response.status_code == 200
    assert "未发现 ERROR 或 CRITICAL" in response.text


def test_download_includes_rotated_file(client: TestClient):
    (client.log_dir / "app.log.1").write_text(  # type: ignore[attr-defined]
        "2026-07-30 09:00:00,000 ERROR proseforge.worker: rotated failure\n", encoding="utf-8"
    )
    response = client.get("/api/v1/logs/errors/download")
    assert response.status_code == 200
    assert "rotated failure" in response.text


def test_download_includes_all_rotated_files_oldest_first(client: TestClient):
    """The handler keeps backupCount=3 rotations; the report must read every
    one of them (highest index = oldest), not just .1."""
    for name, message in (
        ("app.log.3", "oldest rotated failure"),
        ("app.log.2", "middle rotated failure"),
        ("app.log.1", "recent rotated failure"),
    ):
        (client.log_dir / name).write_text(  # type: ignore[attr-defined]
            f"2026-07-30 09:00:00,000 ERROR proseforge.worker: {message}\n", encoding="utf-8"
        )
    _write_app_log(client, ["2026-07-30 10:00:00,000 ERROR proseforge.worker: live failure"])
    response = client.get("/api/v1/logs/errors/download")
    assert response.status_code == 200
    body = response.text
    for message in ("oldest rotated failure", "middle rotated failure", "recent rotated failure", "live failure"):
        assert message in body
    # Entries appear oldest first: .3 < .2 < .1 < live app.log.
    positions = [body.index(message) for message in (
        "oldest rotated failure", "middle rotated failure", "recent rotated failure", "live failure",
    )]
    assert positions == sorted(positions)


def test_download_report_uses_shanghai_time(client: TestClient):
    """报告生成时间统一上海时间（+08:00），不再出现 UTC 字样。"""
    _write_app_log(client, ["2026-07-30 10:00:00,000 INFO proseforge.api: all quiet"])
    response = client.get("/api/v1/logs/errors/download")
    assert response.status_code == 200
    assert "生成时间：" in response.text and "+08:00" in response.text
    assert " UTC" not in response.text


def test_download_requires_auth(tmp_path):
    settings = Settings(
        runtime_profile="native",
        data_dir=str(tmp_path / "data"),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
    )
    with TestClient(create_app(settings)) as anonymous:
        assert anonymous.get("/api/v1/logs/errors/download").status_code == 401


def test_download_requires_admin_role(tmp_path):
    """A plain USER gets 403; the setup owner (ADMIN) still downloads."""
    settings = Settings(
        runtime_profile="native",
        data_dir=str(tmp_path / "data"),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        master_key=MASTER_KEY,
        blob_root=str(tmp_path / "blobs"),
        backup_root=str(tmp_path / "backups"),
        allow_registration=True,
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/v1/auth/setup", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 201
        response = test_client.post("/api/v1/auth/register", json={"email": "guest@example.com", "password": "p" * 12})
        assert response.status_code == 201 and response.json()["role"] == "USER"

        response = test_client.post("/api/v1/auth/login", json={"email": "guest@example.com", "password": "p" * 12})
        assert response.status_code == 200
        assert test_client.get("/api/v1/logs/errors/download").status_code == 403

        response = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "p" * 12})
        assert response.status_code == 200
        assert test_client.get("/api/v1/logs/errors/download").status_code == 200
