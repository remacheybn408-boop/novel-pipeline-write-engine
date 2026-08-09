"""Migration 0050 (model_catalog.owner_id) on sqlite.

Builds users + model_catalog tables without the column, runs the migration
under an alembic operations context twice (idempotency), and asserts the
nullable owner_id column + index exist and pre-existing rows keep NULL
(legacy shared semantics).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

_MIGRATION_PATH = Path(__file__).resolve().parents[2] / "proseforge/infrastructure/database/migrations/versions/0050_model_catalog_owner.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0050", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    # Pre-0050 schema: users table (FK target) + model_catalog without owner_id.
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY, email VARCHAR(320) NOT NULL)"))
        conn.execute(
            sa.text(
                "CREATE TABLE model_catalog ("
                "id VARCHAR(64) PRIMARY KEY, provider VARCHAR(64) NOT NULL,"
                " model_id VARCHAR(200) NOT NULL, capabilities TEXT NOT NULL DEFAULT '{}')"
            )
        )
        conn.execute(sa.text("INSERT INTO users (id, email) VALUES ('u1', 'a@example.com')"))
        conn.execute(sa.text("INSERT INTO model_catalog (id, provider, model_id, capabilities) VALUES ('c1', 'custom', 'legacy-m', '{\"manual\": true}')"))
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
            migration.upgrade()  # idempotent: second run must be a no-op
        conn.commit()
    return engine


def test_upgrade_adds_nullable_owner_id_and_index(engine):
    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("model_catalog")}
    assert "owner_id" in columns
    assert columns["owner_id"]["nullable"] is True
    indexes = {index["name"] for index in inspector.get_indexes("model_catalog")}
    assert "ix_model_catalog_owner_id" in indexes
    fk_targets = {fk["referred_table"] for fk in inspector.get_foreign_keys("model_catalog")}
    assert "users" in fk_targets


def test_existing_rows_keep_null_owner(engine):
    with engine.connect() as conn:
        owner = conn.execute(sa.text("SELECT owner_id FROM model_catalog WHERE id = 'c1'")).scalar_one()
    assert owner is None  # legacy shared row semantics


def test_owned_and_null_rows_roundtrip(engine):
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO model_catalog (id, provider, model_id, capabilities, owner_id) VALUES ('c2', 'custom', 'owned-m', '{}', 'u1')")
        )
        rows = dict(conn.execute(sa.text("SELECT id, owner_id FROM model_catalog")).fetchall())
    assert rows == {"c1": None, "c2": "u1"}


def test_downgrade_drops_column_and_is_idempotent(engine):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()
            migration.downgrade()
        conn.commit()
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("model_catalog")}
    assert "owner_id" not in columns
