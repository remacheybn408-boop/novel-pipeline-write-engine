"""Static checks for migration 0051_clear_retrieval_documents (0048 skip-trap
cleanup): revision chain, the conditional DELETE shape (only trap-state
documents — those with NO active chunks — are removed; a blanket DELETE
would destroy the server's rebuilt index), inspector guard, and an
irreversible-by-design downgrade. The migration is NOT executed here —
upgrade runs only in deployment, after 0050_model_catalog_owner exists.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION_PATH = Path("proseforge/infrastructure/database/migrations/versions/0051_clear_retrieval_documents.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("migration_0051", MIGRATION_PATH)
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
    assert module.revision == "0051_clear_retrieval_documents"
    # 0050_model_catalog_owner is built by a parallel change; only the link
    # is asserted here, not its presence on disk.
    assert module.down_revision == "0050_model_catalog_owner"


def test_conditional_delete_only_clears_trap_state():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade = _function_body(source, "upgrade")
    # Conditional DELETE: documents with no ACTIVE chunk (the 0048 skip
    # trap), never a blanket wipe of the rebuilt index.
    assert "DELETE FROM retrieval_documents WHERE NOT EXISTS" in upgrade
    assert "SELECT 1 FROM retrieval_chunks" in upgrade
    assert "retrieval_chunks.document_id = retrieval_documents.id" in upgrade
    assert "retrieval_chunks.status = 'active'" in upgrade
    # No unconditional delete anywhere in the migration.
    assert "DELETE FROM retrieval_documents;" not in source
    assert "DELETE FROM retrieval_chunks" not in source


def test_inspector_guard_and_irreversible_downgrade():
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    upgrade = _function_body(source, "upgrade")
    # Fresh databases without the retrieval tables must no-op.
    assert "sa.inspect(bind)" in upgrade
    assert "retrieval_documents" in upgrade and "retrieval_chunks" in upgrade
    downgrade = _function_body(source, "downgrade")
    # Data cleanup is not reversible; downgrade must not fabricate rows.
    assert "INSERT" not in downgrade and "DELETE" not in downgrade
