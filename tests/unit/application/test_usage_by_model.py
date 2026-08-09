"""Offline tests for per-model usage aggregation (sqlite in-memory via aiosqlite).

Exercises the real GROUP BY SQL in SqlAlchemyUsageRepository.aggregate_by_model
through UsageQuery.by_model; window resolution follows tools.metrics rules.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.usage.query_usage import UsageQuery
from proseforge.infrastructure.database.models.usage import ModelUsageRecordModel
from proseforge.infrastructure.database.repositories.usage import (
    SqlAlchemyUsageRepository,
)
from tests.conftest import make_fk_engine

NOW = datetime.now(UTC)  # relative windows must track the real clock


def make_row(call_id: str, user: str, provider: str, model_id: str, *, input_tokens=10, output_tokens=5, cached=0, reasoning=0, cost=None, latency=None, created=None) -> ModelUsageRecordModel:
    stamp = created or NOW
    return ModelUsageRecordModel(
        id=call_id, user_id=user, provider=provider, model_id=model_id,
        call_id=call_id, input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached, reasoning_tokens=reasoning,
        total_tokens=input_tokens + output_tokens, cost_usd=cost,
        usage_source="provider", is_final=True, latency_ms=latency,
        created_at=stamp, metadata_json="{}",
    )


@pytest_asyncio.fixture
async def session_factory():
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ModelUsageRecordModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed(factory, rows):
    async with factory() as session:
        session.add_all(rows)
        await session.commit()


async def by_model(factory, user_id, days=7):
    async with factory() as session:
        return await UsageQuery(SqlAlchemyUsageRepository(session)).by_model(user_id, days=days)


@pytest.mark.asyncio
async def test_empty_table_is_safe(session_factory):
    result = await by_model(session_factory, "u1")
    assert result["days"] == 7
    assert result["rows"] == []
    assert result["totals"]["calls"] == 0 and result["totals"]["total_tokens"] == 0
    assert result["totals"]["cost_usd"] is None and result["totals"]["avg_latency_ms"] is None


@pytest.mark.asyncio
async def test_groups_by_provider_and_model(session_factory):
    await seed(session_factory, [
        make_row("c1", "u1", "openai", "gpt-4o", input_tokens=100, output_tokens=50, cost=0.01, latency=100.0),
        make_row("c2", "u1", "openai", "gpt-4o", input_tokens=200, output_tokens=80, cached=20, reasoning=10, cost=0.02, latency=300.0),
        make_row("c3", "u1", "deepseek", "deepseek-chat", input_tokens=500, output_tokens=100, cost=0.005, latency=50.0),
    ])
    result = await by_model(session_factory, "u1")

    assert [row["model_id"] for row in result["rows"]] == ["deepseek-chat", "gpt-4o"]  # total_tokens desc
    rows = {row["model_id"]: row for row in result["rows"]}
    gpt = rows["gpt-4o"]
    assert gpt["provider"] == "openai" and gpt["calls"] == 2
    assert gpt["input_tokens"] == 300 and gpt["output_tokens"] == 130
    assert gpt["cached_input_tokens"] == 20 and gpt["reasoning_tokens"] == 10
    assert gpt["total_tokens"] == 430 and gpt["cost_usd"] == 0.03
    assert gpt["avg_latency_ms"] == 200.0
    assert gpt["last_used_at"]

    totals = result["totals"]
    assert totals["calls"] == 3 and totals["total_tokens"] == 1030
    assert totals["cost_usd"] == 0.035
    assert totals["avg_latency_ms"] == round((200.0 * 2 + 50.0) / 3, 1)


@pytest.mark.asyncio
async def test_same_model_id_under_two_providers_stays_separate(session_factory):
    await seed(session_factory, [
        make_row("c1", "u1", "openai", "shared-model", input_tokens=10, output_tokens=5),
        make_row("c2", "u1", "azure", "shared-model", input_tokens=20, output_tokens=5),
    ])
    result = await by_model(session_factory, "u1")
    assert {(row["provider"], row["model_id"]) for row in result["rows"]} == {("openai", "shared-model"), ("azure", "shared-model")}
    assert result["totals"]["calls"] == 2


@pytest.mark.asyncio
async def test_window_excludes_old_rows(session_factory):
    await seed(session_factory, [
        make_row("old", "u1", "openai", "gpt-4o", created=NOW - timedelta(days=20)),
        make_row("new", "u1", "openai", "gpt-4o", created=NOW - timedelta(days=2)),
    ])
    result = await by_model(session_factory, "u1", days=7)
    assert result["totals"]["calls"] == 1


@pytest.mark.asyncio
async def test_user_isolation(session_factory):
    await seed(session_factory, [
        make_row("c1", "u1", "openai", "gpt-4o"),
        make_row("c2", "u2", "openai", "gpt-4o"),
    ])
    result = await by_model(session_factory, "u1")
    assert result["totals"]["calls"] == 1


@pytest.mark.asyncio
async def test_invalid_window_falls_back_to_default(session_factory):
    await seed(session_factory, [make_row("c1", "u1", "openai", "gpt-4o", created=NOW - timedelta(days=2))])
    result = await by_model(session_factory, "u1", days=365)
    assert result["days"] == 7
    assert result["totals"]["calls"] == 1
