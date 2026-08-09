"""Migration 0039 (characters + chapter_versions.summary) on sqlite."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "proseforge/infrastructure/database/migrations/versions/0039_characters_and_version_summary.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0039", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(128) NOT NULL)"))
        conn.execute(sa.text(
            "CREATE TABLE chapter_versions (id VARCHAR(64) PRIMARY KEY, chapter_id VARCHAR(64) NOT NULL, "
            "version_no INTEGER NOT NULL, content TEXT NOT NULL, content_hash VARCHAR(64) NOT NULL, "
            "word_count INTEGER NOT NULL)"
        ))
        conn.execute(sa.text("INSERT INTO chapter_versions (id, chapter_id, version_no, content, content_hash, word_count) VALUES ('v1', 'c1', 1, '正文', 'h', 2)"))
    yield engine
    engine.dispose()


def _run(engine, migration, direction: str) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(migration, direction)()
        conn.commit()


def test_upgrade_creates_characters_and_summary(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")

    inspector = sa.inspect(engine)
    assert "characters" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("characters")}
    assert {"id", "project_id", "name", "aliases_json", "summary", "role",
            "first_seen_chapter", "last_seen_chapter", "status", "source",
            "confidence", "created_at", "updated_at"} <= columns
    fks = inspector.get_foreign_keys("characters")
    assert any(fk["referred_table"] == "projects" for fk in fks)
    uniques = inspector.get_unique_constraints("characters")
    assert any(set(uc["column_names"]) == {"project_id", "name"} for uc in uniques)

    version_columns = {c["name"] for c in inspector.get_columns("chapter_versions")}
    assert "summary" in version_columns
    # Existing rows get the server default, not NULL.
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT summary FROM chapter_versions WHERE id = 'v1'")).scalar() == ""


def test_upgrade_is_idempotent(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "upgrade")
    assert "characters" in sa.inspect(engine).get_table_names()


def test_downgrade(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "downgrade")
    inspector = sa.inspect(engine)
    assert "characters" not in inspector.get_table_names()
    assert "summary" not in {c["name"] for c in inspector.get_columns("chapter_versions")}
