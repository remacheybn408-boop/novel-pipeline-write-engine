from __future__ import annotations

import io
import tarfile

import pytest

from proseforge.operations.backup import BackupService


def test_backup_restore_rejects_tampered_member(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "chapter.txt").write_text("original", encoding="utf-8")
    service = BackupService(tmp_path / "backups")
    created = service.create(source, database_dump=b"CREATE TABLE projects (id text);")

    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(created.archive, "r:gz") as source_tar, tarfile.open(tampered, "w:gz") as target_tar:
        for member in source_tar.getmembers():
            payload = source_tar.extractfile(member).read() if member.isfile() else None
            if member.name == "chapter.txt":
                payload = b"changed"
                member.size = len(payload)
            target_tar.addfile(member, io.BytesIO(payload) if payload is not None else None)

    with pytest.raises(ValueError, match="checksum mismatch"):
        service.verify(tampered)


def test_database_restore_requires_staging_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    service = BackupService(tmp_path / "backups")
    created = service.create(source, database_dump=b"SELECT 1;")

    with pytest.raises(ValueError, match="staging database"):
        service.restore_database(created.archive, "postgresql://proseforge/live")


def test_database_restore_runs_only_against_staging(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    service = BackupService(tmp_path / "backups")
    created = service.create(source, database_dump=b"SELECT 1;\n")
    calls = []

    class Result:
        returncode = 0
        stderr = b""

    monkeypatch.setattr("proseforge.operations.backup.subprocess.run", lambda args, **kwargs: calls.append(args) or Result())
    service.restore_database(created.archive, "postgresql://proseforge/proseforge_staging")

    assert calls and calls[0][0] == "psql"
    assert "proseforge_staging" in calls[0][2]
