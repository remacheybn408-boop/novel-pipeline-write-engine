"""Migration 0035 (project FK backfill) on sqlite.

The full alembic chain is PostgreSQL-only in places, so this builds the
pre-0035 schema by cloning current metadata minus the new FK constraints,
seeds valid + orphan rows, then runs the migration's upgrade()/downgrade()
directly under an alembic operations context.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from proseforge.infrastructure.database import (
    models,  # noqa: F401  # register metadata
)
from proseforge.infrastructure.database.base import Base

_MIGRATION_PATH = Path(__file__).resolve().parents[2] / "proseforge/infrastructure/database/migrations/versions/0035_project_fk_backfill.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0035", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_0035_metadata(migration) -> sa.MetaData:
    """Clone current metadata and strip exactly the FKs 0035 adds."""
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    for table_name, _constraint, column, _ref in migration._FOREIGN_KEYS:
        # The cloned metadata reflects CURRENT models; frozen 0035 entries
        # whose table was dropped later (embeddings, by 0037) have nothing
        # to strip.
        if table_name not in metadata.tables:
            continue
        table = metadata.tables[table_name]
        for constraint in list(table.constraints):
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.column_keys == [column]:
                table.constraints.discard(constraint)
    return metadata


def _seed(engine) -> None:
    """One valid p1 subtree + orphans at every chain level."""
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO projects (id, owner_id, slug, title, genre, style, language, status, mode) VALUES ('p1', 'u1', 'p1', 'P1', '', '', 'zh-CN', 'ACTIVE', 'work')"))
        # Valid conversation chain under p1.
        conn.execute(sa.text("INSERT INTO conversations (id, project_id, title) VALUES ('cv-ok', 'p1', 't')"))
        conn.execute(sa.text("INSERT INTO conversation_branches (id, conversation_id, name) VALUES ('b-ok', 'cv-ok', 'main')"))
        conn.execute(sa.text("INSERT INTO messages (id, branch_id, role, content, sequence_no, status) VALUES ('m-ok', 'b-ok', 'user', 'hi', 1, 'COMPLETED')"))
        conn.execute(sa.text("INSERT INTO message_chunks (id, message_id, chunk_index, event_type, content) VALUES ('mc-ok', 'm-ok', 0, 'delta', 'x')"))
        # Orphans: ghost project conversation chain + dangling chunk.
        conn.execute(sa.text("INSERT INTO conversations (id, project_id, title) VALUES ('cv-ghost', 'ghost-project', 't')"))
        conn.execute(sa.text("INSERT INTO conversation_branches (id, conversation_id, name) VALUES ('b-ghost', 'cv-ghost', 'main')"))
        conn.execute(sa.text("INSERT INTO messages (id, branch_id, role, content, sequence_no, status) VALUES ('m-ghost', 'b-ghost', 'user', 'hi', 1, 'COMPLETED')"))
        conn.execute(sa.text("INSERT INTO message_chunks (id, message_id, chunk_index, event_type, content) VALUES ('mc-ghost', 'm-ghost', 0, 'delta', 'x')"))
        conn.execute(sa.text("INSERT INTO message_chunks (id, message_id, chunk_index, event_type, content) VALUES ('mc-dangling', 'no-such-message', 0, 'delta', 'x')"))
        # Message-stream event (DatabaseEventStream stores message: topic
        # suffixes as conversation_id): no parent conversation exists BY
        # DESIGN, and the migration must keep the row (no FK, no cleanup).
        conn.execute(sa.text("INSERT INTO conversation_events (id, conversation_id, event_sequence, event_type, payload) VALUES ('ce-msg-stream', 'some-message-id', 1, 'e', '{}')"))
        # Orphan chapter chain + flat tables.
        conn.execute(sa.text("INSERT INTO chapters (id, project_id, chapter_no, title, status) VALUES ('ch-ghost', 'ghost-project', 1, 'c', 'PLANNED')"))
        conn.execute(sa.text("INSERT INTO chapter_versions (id, chapter_id, version_no, content, content_hash, word_count) VALUES ('cvv-ghost', 'ch-ghost', 1, 'x', 'h', 1)"))
        conn.execute(sa.text("INSERT INTO revision_proposals (id, chapter_id, base_version_id, before_hash, after_text, after_hash, rationale, status, hunks_json, affected_facts_json, conflict_status, guard_status, created_at) VALUES ('rp-dangling', 'no-such-chapter', 'v', 'a', 'b', 'c', 'r', 'PROPOSED', '[]', '[]', 'clear', 'clear', '2026-01-01 00:00:00')"))
        conn.execute(sa.text("INSERT INTO workflow_runs (id, project_id, workflow_type, status) VALUES ('wr-ghost', 'ghost-project', 'NOVEL', 'QUEUED')"))
        conn.execute(sa.text("INSERT INTO workflow_steps (id, workflow_run_id, idempotency_key, status) VALUES ('ws-ghost', 'wr-ghost', 'k', 'DONE')"))
        conn.execute(sa.text("INSERT INTO agent_runs (id, user_id, project_id, goal_hash, graph_revision, status, policy_version, budget_limit, budget_used, event_cursor, created_at, updated_at) VALUES ('ar-ghost', 'u1', 'ghost-project', 'g', 1, 'PENDING', 'v3-policy-1', 0, 0, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"))
        conn.execute(sa.text("INSERT INTO agent_tasks (id, run_id, task_key, role, status, attempts, token_budget, depends_on, retryable_attempts) VALUES ('at-ghost', 'ar-ghost', 't', 'r', 'PENDING', 0, 1, '[]', 0)"))
        conn.execute(sa.text("INSERT INTO agent_memories (id, project_id, run_id, memory_key, value, source_artifact_id, status) VALUES ('am-ghost', 'ghost-project', 'ar-ghost', 'k', 'v', 's', 'PENDING')"))
        conn.execute(sa.text("INSERT INTO outlines (id, project_id, title, status, payload, missing_questions, confirmed) VALUES ('o-ghost', 'ghost-project', 'o', 'UPLOADED', '{}', '[]', 0)"))
        conn.execute(sa.text("INSERT INTO outline_versions (id, outline_id, version_no, payload) VALUES ('ov-ghost', 'o-ghost', 1, '{}')"))
        conn.execute(sa.text("INSERT INTO attachments (id, project_id, filename, sha256, storage_key) VALUES ('att-ghost', 'ghost-project', 'a.txt', 's', 'k')"))
        conn.execute(sa.text("INSERT INTO embeddings (id, project_id, source_type, source_id, chunk_index, embedding_model, vector_json) VALUES ('eb-ghost', 'ghost-project', 's', 's', 0, 'e', '[]')"))


def _count(conn, table: str) -> int:
    return conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


@pytest.fixture
def migrated(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    _pre_0035_metadata(migration).create_all(engine)
    with engine.begin() as conn:
        # Dropped by 0037 but present in the 0034-era schema this test
        # simulates, so create it by hand (frozen history, no ORM model).
        conn.execute(sa.text(
            "CREATE TABLE embeddings (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, "
            "source_type VARCHAR(64) NOT NULL, source_id VARCHAR(64) NOT NULL, chunk_index INTEGER NOT NULL, "
            "embedding_model VARCHAR(200) NOT NULL, vector_json TEXT NOT NULL)"
        ))
    _seed(engine)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
        conn.commit()
    return migration, engine


def test_upgrade_creates_named_foreign_keys(migrated):
    migration, engine = migrated
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        for table, constraint, _column, _ref in migration._FOREIGN_KEYS:
            names = {fk["name"] for fk in inspector.get_foreign_keys(table)}
            assert constraint in names, f"{table} missing {constraint}"


def test_upgrade_deletes_orphans_keeps_valid_rows(migrated):
    _migration, engine = migrated
    with engine.connect() as conn:
        assert _count(conn, "conversations") == 1  # cv-ok kept, cv-ghost gone
        assert _count(conn, "conversation_branches") == 1
        assert _count(conn, "messages") == 1
        assert _count(conn, "message_chunks") == 1
        # Message-stream events are kept by design (no FK possible on the
        # polymorphic stream key).
        assert _count(conn, "conversation_events") == 1
        assert _count(conn, "chapters") == 0
        assert _count(conn, "chapter_versions") == 0
        assert _count(conn, "revision_proposals") == 0
        assert _count(conn, "workflow_runs") == 0
        assert _count(conn, "workflow_steps") == 0
        assert _count(conn, "agent_runs") == 0
        assert _count(conn, "agent_tasks") == 0
        assert _count(conn, "agent_memories") == 0
        assert _count(conn, "outlines") == 0
        assert _count(conn, "outline_versions") == 0
        assert _count(conn, "attachments") == 0
        assert _count(conn, "embeddings") == 0


def test_upgrade_cascade_delete_works(migrated):
    _migration, engine = migrated
    with engine.connect() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text("DELETE FROM projects WHERE id = 'p1'"))
        assert _count(conn, "conversations") == 0
        assert _count(conn, "conversation_branches") == 0
        assert _count(conn, "messages") == 0
        assert _count(conn, "message_chunks") == 0
        conn.commit()


def test_downgrade_drops_foreign_keys(migrated, tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration, engine = migrated
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()
        conn.commit()
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        for table, constraint, _column, _ref in migration._FOREIGN_KEYS:
            names = {fk["name"] for fk in inspector.get_foreign_keys(table)}
            assert constraint not in names, f"{table} still has {constraint}"
