from proseforge.operations.backup import BackupService


def test_backup_can_be_verified(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "chapter.txt").write_text("content", encoding="utf-8")
    service = BackupService(tmp_path / "backups")
    created = service.create(source)
    verified = service.verify(created.archive)
    assert verified.files == 1
    assert verified.sha256 == created.sha256


def test_backup_restore_is_staged_and_safe(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "project.json").write_text('{"title":"Moonlit Archive"}', encoding="utf-8")
    service = BackupService(tmp_path / "backups")

    created = service.create(source)
    restored = tmp_path / "staging"
    result = service.restore(created.archive, restored)

    assert result.files == 1
    assert (restored / "project.json").read_text(encoding="utf-8") == '{"title":"Moonlit Archive"}'


def test_backup_contains_database_dump_and_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    service = BackupService(tmp_path / "backups")
    created = service.create(source, database_dump=b"CREATE TABLE projects (...);", application_version="1.0", migration_revision="0004_users")
    verified = service.verify(created.archive)
    assert verified.metadata["migration_revision"] == "0004_users"
    assert verified.metadata["entries"][-1]["path"] == "database.dump"
