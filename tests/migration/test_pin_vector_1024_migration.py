"""Static checks for migration 0048_pin_vector_1024 (bge-m3 convergence):
revision chain, locked ordering (clear chunks -> pin vector(1024) -> HNSW),
and the pgvector cosine opclass. The migration is NOT executed here —
upgrade runs only in deployment, after 0047_recap_rollups exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = Path("proseforge/infrastructure/database/migrations/versions/0048_pin_vector_1024.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("migration_0048", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function_body(source: str, name: str) -> str:
    start = source.index(f"def {name}()")
    rest = source[start:]
    next_def = rest.find("\ndef ", 1)
    return rest if next_def == -1 else rest[:next_def]


def test_revision_chain():
    module = _load_module()
    assert module.revision == "0048_pin_vector_1024"
    # 0047_recap_rollups is built by a parallel change; only the link is
    # asserted here, not its presence on disk.
    assert module.down_revision == "0047_recap_rollups"


def test_locked_operation_order_and_pgvector_syntax():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade = _function_body(source, "upgrade")
    clear = upgrade.index("DELETE FROM retrieval_chunks")
    pin = upgrade.index("ALTER TABLE retrieval_chunks ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM})")
    hnsw = upgrade.index("USING hnsw (embedding vector_cosine_ops)")
    # Locked order: force-clear BEFORE pinning the dimension, HNSW after.
    assert clear < pin < hnsw
    assert "EMBEDDING_DIM = 1024" in source
    assert "ix_retrieval_chunks_embedding_hnsw" in source
    # Non-PostgreSQL dialects keep the JSON fallback: the migration no-ops.
    assert 'bind.dialect.name != "postgresql"' in upgrade


def test_downgrade_drops_index_and_unpins():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    downgrade = _function_body(source, "downgrade")
    assert "DROP INDEX" in downgrade
    # Downgrade returns to the dimensionless vector type.
    assert '"ALTER TABLE retrieval_chunks ALTER COLUMN embedding TYPE vector"' in downgrade
