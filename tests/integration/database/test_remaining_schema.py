import pytest
from sqlalchemy import inspect

EXPECTED = {
    "provider_credentials", "model_catalog", "model_profiles", "attachments", "artifacts",
    "context_items", "context_snapshots", "workflow_runs", "workflow_steps",
    "workflow_events", "model_calls", "quality_reports", "health_checks", "audit_logs",
}


@pytest.mark.asyncio
async def test_remaining_schema_is_present(session_factory):
    bind = session_factory.kw["bind"]
    async with bind.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    assert EXPECTED <= tables
