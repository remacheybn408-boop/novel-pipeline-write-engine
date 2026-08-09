import json
import sqlite3

import pytest

from proseforge.infrastructure.legacy_import.importer import LegacyImporter


@pytest.mark.asyncio
async def test_legacy_import_preserves_latest_content_hash(tmp_path):
    slot = tmp_path / "slot-1"
    (slot / "outlines").mkdir(parents=True)
    (slot / "reports").mkdir()
    (slot / "project.json").write_text(json.dumps({"title": "Imported"}), encoding="utf-8")
    connection = sqlite3.connect(slot / "novel.db")
    connection.executescript("""
        CREATE TABLE chapters (id TEXT PRIMARY KEY, chapter_no INTEGER, title TEXT);
        CREATE TABLE chapter_versions (chapter_id TEXT, version_no INTEGER, content TEXT);
        INSERT INTO chapters VALUES ('c1', 1, 'One'), ('c2', 2, 'Two');
        INSERT INTO chapter_versions VALUES ('c1', 1, 'old'), ('c1', 2, 'latest');
    """)
    connection.commit()
    connection.close()
    report = await LegacyImporter(tmp_path / "archive").import_workspace(tmp_path)
    assert report.status == "COMPLETED"
    assert report.projects_imported == 1
    assert report.chapters_imported == 2
    assert report.hash_mismatches == ()
