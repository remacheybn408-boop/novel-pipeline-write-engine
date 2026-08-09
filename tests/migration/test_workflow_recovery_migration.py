from __future__ import annotations

from pathlib import Path


def test_workflow_recovery_migration_declares_missing_table_repair():
    source = Path("proseforge/infrastructure/database/migrations/versions/0006_workflow_recovery.py").read_text(encoding="utf-8")
    assert "checkfirst=True" in source
    assert '"workflow_runs"' in source
