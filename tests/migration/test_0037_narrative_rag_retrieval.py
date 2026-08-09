"""Migration 0037 (narrative RAG retrieval schema) on sqlite.

Builds the pre-0037 schema (users/projects + the legacy embeddings table
with dirty rows), runs the migration under an alembic operations context,
and asserts: new tables exist with the expected columns/FKs, the legacy
embeddings table is dropped, the migration is idempotent, and downgrade
restores the legacy shell.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.project import ProjectModel

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "proseforge/infrastructure/database/migrations/versions/0037_narrative_rag_retrieval.py"
)

_NEW_TABLES = {
    "user_preferences",
    "retrieval_documents",
    "retrieval_chunks",
    "retrieval_jobs",
    "retrieval_runs",
    "canon_conflicts",
}

_LEGACY_EMBEDDINGS_DDL = (
    "CREATE TABLE embeddings (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL "
    "REFERENCES projects(id) ON DELETE CASCADE, source_type VARCHAR(64) NOT NULL, "
    "source_id VARCHAR(64) NOT NULL, chunk_index INTEGER NOT NULL, "
    "embedding_model VARCHAR(200) NOT NULL, vector_json TEXT NOT NULL)"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0037", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    UserModel.__table__.create(engine)
    ProjectModel.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(_LEGACY_EMBEDDINGS_DDL))
        conn.execute(sa.text("INSERT INTO users (id, email, password_hash, role, session_version) VALUES ('u1', 'u@x.com', 'h', 'USER', 1)"))
        conn.execute(sa.text("INSERT INTO projects (id, owner_id, slug, title, genre, style, language, status, mode) VALUES ('p1', 'u1', 's', 't', '', '', 'zh-CN', 'ACTIVE', 'work')"))
        conn.execute(sa.text("INSERT INTO embeddings (id, project_id, source_type, source_id, chunk_index, embedding_model, vector_json) VALUES ('e1', 'p1', 'chapter', 'c1', 0, 'old-model', '[0.1]')"))
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


def test_upgrade_creates_retrieval_schema_and_drops_legacy(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")

    inspector = sa.inspect(engine)
    names = set(inspector.get_table_names())
    assert _NEW_TABLES <= names
    assert "embeddings" not in names

    chunk_columns = {column["name"] for column in inspector.get_columns("retrieval_chunks")}
    assert {"id", "project_id", "document_id", "chunk_index", "content", "embedding",
            "embedding_model", "embedding_version", "content_hash", "status"} <= chunk_columns
    # sqlite fallback stores the embedding as JSON text.
    embedding_type = next(c for c in inspector.get_columns("retrieval_chunks") if c["name"] == "embedding")
    assert isinstance(embedding_type["type"], sa.Text)

    chunk_fks = inspector.get_foreign_keys("retrieval_chunks")
    assert any(fk["referred_table"] == "projects" and fk["referred_columns"] == ["id"] for fk in chunk_fks)
    assert any(fk["referred_table"] == "retrieval_documents" for fk in chunk_fks)

    preference_uniques = inspector.get_unique_constraints("user_preferences")
    assert any(set(uc["column_names"]) == {"user_id", "key"} for uc in preference_uniques)


def test_upgrade_is_idempotent(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "upgrade")
    assert _NEW_TABLES <= set(sa.inspect(engine).get_table_names())


def test_downgrade_restores_legacy_shell(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "downgrade")

    names = set(sa.inspect(engine).get_table_names())
    assert "embeddings" in names
    assert not (_NEW_TABLES & names)
    # Legacy rows are not restored (documented data loss); the shell is empty.
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM embeddings")).scalar() == 0
