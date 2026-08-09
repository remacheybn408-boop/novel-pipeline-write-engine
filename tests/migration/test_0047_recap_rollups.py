"""Migration 0047 (recap_rollups) on sqlite: table shape, unique
constraint, idempotent upgrade, downgrade drops the table."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from proseforge.infrastructure.database.models.auth import UserModel
from proseforge.infrastructure.database.models.project import ProjectModel

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "proseforge/infrastructure/database/migrations/versions/0047_recap_rollups.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0047", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    UserModel.__table__.create(engine)
    ProjectModel.__table__.create(engine)
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


def test_upgrade_creates_recap_rollups(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")

    inspector = sa.inspect(engine)
    assert "recap_rollups" in set(inspector.get_table_names())
    columns = {column["name"] for column in inspector.get_columns("recap_rollups")}
    assert columns == {
        "id", "project_id", "user_id", "level", "span_start", "span_end",
        "content", "source_version_ids", "stale", "created_at", "updated_at",
    }
    fks = inspector.get_foreign_keys("recap_rollups")
    assert any(fk["referred_table"] == "projects" and fk["referred_columns"] == ["id"] for fk in fks)
    uniques = inspector.get_unique_constraints("recap_rollups")
    assert any(set(uc["column_names"]) == {"project_id", "level", "span_start"} for uc in uniques)


def test_upgrade_is_idempotent(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "upgrade")
    assert "recap_rollups" in set(sa.inspect(engine).get_table_names())


def test_downgrade_drops_table(engine):
    migration = _load_migration()
    _run(engine, migration, "upgrade")
    _run(engine, migration, "downgrade")
    assert "recap_rollups" not in set(sa.inspect(engine).get_table_names())


def test_revision_chain():
    migration = _load_migration()
    assert migration.revision == "0047_recap_rollups"
    assert migration.down_revision == "0046_agent_task_retry_backoff"
