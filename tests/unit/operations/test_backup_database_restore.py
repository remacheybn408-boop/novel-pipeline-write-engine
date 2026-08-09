from subprocess import CompletedProcess

import pytest

from proseforge.operations.backup import BackupService


def test_database_restore_requires_staging_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    archive = BackupService(tmp_path / "backups").create(source, database_dump=b"select 1;").archive
    with pytest.raises(ValueError, match="staging"):
        BackupService(tmp_path / "backups").restore_database(archive, "postgresql://user:pass@db/proseforge")


def test_database_restore_executes_verified_dump_into_staging(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    service = BackupService(tmp_path / "backups")
    archive = service.create(source, database_dump=b"select 1;").archive
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        return CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("proseforge.operations.backup.subprocess.run", run)
    service.restore_database(archive, "postgresql+asyncpg://user:pass@db/proseforge_staging")
    assert captured["command"][:4] == ["psql", "--dbname", "postgresql://user:pass@db/proseforge_staging", "--set"]
