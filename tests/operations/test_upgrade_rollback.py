from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from proseforge.operations.backup import BackupService
from proseforge.operations.upgrade import UpgradeBusyError, run_upgrade


def _seed_data(data: Path) -> None:
    (data / "projects").mkdir(parents=True)
    (data / "projects" / "demo.json").write_text("project-before", encoding="utf-8")
    (data / "chapters").mkdir()
    (data / "chapters" / "ch1.txt").write_text("chapter-before", encoding="utf-8")
    (data / "versions").mkdir()
    (data / "versions" / "v1.json").write_text("version-before", encoding="utf-8")
    (data / "usage.json").write_text("usage-before", encoding="utf-8")


def _reports(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("upgrade-report-*.json"))


def test_upgrade_failure_restores_data_from_verified_backup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    marker = data / "project.json"
    marker.write_text("before", encoding="utf-8")

    def migrate():
        marker.write_text("after", encoding="utf-8")
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        run_upgrade(data_dir=data, backup_dir=tmp_path / "backups", migrate=migrate)
    assert marker.read_text(encoding="utf-8") == "before"
    assert not (data / ".upgrade.lock").exists()


def test_upgrade_lock_rejects_second_process(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    lock = data / ".upgrade.lock"
    lock.write_text("other", encoding="utf-8")
    with pytest.raises(UpgradeBusyError):
        run_upgrade(data_dir=data, backup_dir=tmp_path / "backups", migrate=lambda: None)


def test_successful_upgrade_writes_report_and_preserves_backup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed_data(data)
    backups = tmp_path / "backups"

    def migrate():
        (data / "chapters" / "ch1.txt").write_text("chapter-after", encoding="utf-8")

    report_path = run_upgrade(data_dir=data, backup_dir=backups, migrate=migrate)
    assert report_path == _reports(backups)[-1]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"] == "success"
    assert report["version_before"] and report["version_after"]
    assert Path(report["backup"]).is_file()
    assert (data / "chapters" / "ch1.txt").read_text(encoding="utf-8") == "chapter-after"
    # The backup captured the pre-upgrade state of every data file.
    staging = tmp_path / "staging"
    BackupService(backups).restore(report["backup"], staging)
    assert (staging / "projects" / "demo.json").read_text(encoding="utf-8") == "project-before"
    assert (staging / "chapters" / "ch1.txt").read_text(encoding="utf-8") == "chapter-before"
    assert (staging / "versions" / "v1.json").read_text(encoding="utf-8") == "version-before"
    assert (staging / "usage.json").read_text(encoding="utf-8") == "usage-before"


def test_corrupt_backup_checksum_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed_data(data)
    service = BackupService(tmp_path / "backups")
    archive = Path(service.create(data).archive)
    tampered = tmp_path / "tampered"
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(tampered, filter="data")
    (tampered / "usage.json").write_text("tampered", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as repacked:
        for path in sorted(tampered.rglob("*")):
            if path.is_file():
                repacked.add(path, arcname=path.relative_to(tampered))
    with pytest.raises(ValueError, match="checksum mismatch"):
        service.verify(archive)
    with pytest.raises(ValueError, match="checksum mismatch"):
        service.restore(archive, tmp_path / "restore")


def test_migrate_failure_writes_sanitized_report(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed_data(data)
    backups = tmp_path / "backups"

    def migrate():
        (data / "usage.json").write_text("usage-after", encoding="utf-8")
        raise RuntimeError("cannot connect to postgresql://user:secret-password@db.internal:5432/proseforge")

    with pytest.raises(RuntimeError):
        run_upgrade(data_dir=data, backup_dir=backups, migrate=migrate)
    assert (data / "usage.json").read_text(encoding="utf-8") == "usage-before"
    reports = _reports(backups)
    assert len(reports) == 1
    raw = reports[0].read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["result"] == "failed"
    assert report["error_type"] == "RuntimeError"
    assert report["rolled_back"] is True
    assert "secret-password" not in raw
    assert "db.internal" not in raw


def test_rollback_restores_sqlite_and_runs_integrity_check(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "proseforge.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO t (body) VALUES ('before')")
    backups = tmp_path / "backups"

    def migrate():
        database.write_bytes(b"not a sqlite database")
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        run_upgrade(data_dir=data, backup_dir=backups, migrate=migrate, database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT body FROM t").fetchone()[0] == "before"
    report = json.loads(_reports(backups)[0].read_text(encoding="utf-8"))
    assert report["result"] == "failed"
    assert report["integrity_check"] == "ok"


def test_stop_hook_runs_before_migrate(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    calls: list[str] = []
    run_upgrade(data_dir=data, backup_dir=tmp_path / "backups", stop=lambda: calls.append("stop"), migrate=lambda: calls.append("migrate"), start=lambda: calls.append("start"))
    assert calls == ["stop", "migrate", "start"]
    assert not (data / ".upgrade.lock").exists()
