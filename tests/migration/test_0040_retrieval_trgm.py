"""Migration 0040 (pg_trgm + GIN index) — sqlite path is a guarded no-op."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "proseforge/infrastructure/database/migrations/versions/0040_retrieval_trgm.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0040", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_and_downgrade_are_noops(tmp_path):
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE retrieval_chunks (id VARCHAR(64) PRIMARY KEY, content TEXT)"))

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
            migration.upgrade()  # idempotent
            migration.downgrade()
        conn.commit()
    # No trigram index attempted on sqlite; schema untouched.
    indexes = sa.inspect(engine).get_indexes("retrieval_chunks")
    assert indexes == []
    engine.dispose()
