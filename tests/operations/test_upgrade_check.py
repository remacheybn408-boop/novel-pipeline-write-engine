from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from proseforge.cli.main import main
from proseforge.operations.upgrade import check_upgrade, head_alembic_revision


def _sqlite_url(path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_check_ready_and_pending_on_fresh_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    report = check_upgrade(data_dir=data, backup_dir=tmp_path / "backups", database_url=_sqlite_url(data / "proseforge.sqlite3"))
    assert report["status"] == "ready"
    assert report["pending"] is True
    assert report["current_revision"] is None
    assert report["head_revision"] == head_alembic_revision()
    assert report["free_bytes"] > 0
    # The check must not create a database file as a side effect.
    assert not (data / "proseforge.sqlite3").exists()


def test_check_up_to_date_when_database_is_stamped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "proseforge.sqlite3"
    head = head_alembic_revision()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (head,))
    report = check_upgrade(data_dir=data, backup_dir=tmp_path / "backups", database_url=_sqlite_url(database))
    assert report["status"] == "ready"
    assert report["pending"] is False
    assert report["current_revision"] == head


def test_check_blocked_by_upgrade_lock(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".upgrade.lock").write_text("held", encoding="utf-8")
    report = check_upgrade(data_dir=data, backup_dir=tmp_path / "backups")
    assert report["status"] == "blocked"
    assert "lock" in report["reason"]


def test_sync_url_downgrades_async_drivers():
    from proseforge.operations.upgrade import _sync_url

    assert _sync_url("sqlite+aiosqlite:////data/x.sqlite3") == "sqlite:////data/x.sqlite3"
    assert _sync_url("postgresql+asyncpg://u:p@db/x") == "postgresql+psycopg://u:p@db/x"
    assert _sync_url("postgresql+psycopg://u:p@db/x") == "postgresql+psycopg://u:p@db/x"


def test_check_blocked_when_data_dir_missing(tmp_path):
    report = check_upgrade(data_dir=tmp_path / "missing", backup_dir=tmp_path / "backups")
    assert report["status"] == "blocked"
    assert report["free_bytes"] == 0
    assert "reason" in report


def test_cli_upgrade_check_json_keys_stable(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    assert main(["upgrade", "--check", "--data-dir", str(data), "--backup-dir", str(tmp_path / "backups")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"status", "data_dir", "backup_dir", "current_revision", "head_revision", "pending", "free_bytes"}
    assert output["status"] == "ready"
    assert "secret" not in json.dumps(output).lower()


def test_cli_upgrade_check_blocked_by_lock_returns_nonzero(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".upgrade.lock").write_text("held", encoding="utf-8")
    assert main(["upgrade", "--check", "--data-dir", str(data), "--backup-dir", str(tmp_path / "backups")]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert "reason" in output


def test_cli_upgrade_rejects_busy_instance(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".upgrade.lock").write_text("held", encoding="utf-8")
    assert main(["upgrade", "--data-dir", str(data), "--backup-dir", str(tmp_path / "backups")]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"


def test_cli_upgrade_success_prints_report_path(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    backups = tmp_path / "backups"
    monkeypatch.setattr("proseforge.operations.upgrade.alembic_migration_callable", lambda url: lambda: None)
    assert main(["upgrade", "--data-dir", str(data), "--backup-dir", str(backups)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "upgraded"
    report_path = Path(output["report"])
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "success"
    assert report["backup"]
