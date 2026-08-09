"""Offline tests for tool metrics aggregation (sqlite in-memory via aiosqlite).

The percentile path falls back to Python on sqlite (percentile_cont is
PostgreSQL-only in production); everything else exercises the real SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.tools.metrics import (
    build_tool_metrics,
    percentile,
    resolve_window,
)
from proseforge.infrastructure.database.models.tool_call import ToolCallLogModel
from tests.conftest import make_fk_engine

NOW = datetime.now(UTC)  # relative windows must track the real clock


def make_row(call_id: str, user: str, tool: str, status: str, *, error_class=None, cache_hit=False, duration=100.0, summary="", created=None) -> ToolCallLogModel:
    stamp = created or NOW
    return ToolCallLogModel(
        call_id=call_id, message_id="m1", conversation_id="c1", user_id=user,
        tool_name=tool, status=status, error_class=error_class, params_json="{}",
        result_summary=summary, result_bytes=len(summary), cache_hit=cache_hit,
        attempt=1, duration_ms=duration, started_at=stamp, finished_at=stamp,
        resource_json="{}", created_at=stamp,
    )


@pytest_asyncio.fixture
async def session_factory():
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ToolCallLogModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed(factory, rows):
    async with factory() as session:
        session.add_all(rows)
        await session.commit()


@pytest.mark.asyncio
async def test_empty_table_is_safe(session_factory):
    async with session_factory() as session:
        metrics = await build_tool_metrics(session, user_id="u1", days=7)
    assert metrics["total_calls"] == 0
    assert metrics["success_rate"] == 0.0 and metrics["timeout_rate"] == 0.0 and metrics["cache_hit_rate"] == 0.0
    assert metrics["tools"] == [] and metrics["recent_failures"] == []
    assert metrics["window_days"] == 7 and metrics["since"]


@pytest.mark.asyncio
async def test_aggregation_and_ratios(session_factory):
    await seed(session_factory, [
        make_row("c1", "u1", "read_page", "done", duration=100.0),
        make_row("c2", "u1", "read_page", "done", duration=200.0, cache_hit=True),
        make_row("c3", "u1", "read_page", "done", duration=300.0),
        make_row("c4", "u1", "read_page", "failed", error_class="upstream", duration=400.0, summary="x" * 200),
        make_row("c5", "u1", "search_web", "done", duration=50.0),
        make_row("c6", "u1", "search_web", "failed", error_class="timeout", duration=60.0, summary="timed out"),
    ])
    async with session_factory() as session:
        metrics = await build_tool_metrics(session, user_id="u1", days=7)

    assert metrics["total_calls"] == 6
    assert metrics["success_rate"] == round(4 / 6, 4)
    assert metrics["timeout_rate"] == round(1 / 6, 4)
    assert metrics["cache_hit_rate"] == round(1 / 6, 4)

    tools = {tool["tool_name"]: tool for tool in metrics["tools"]}
    assert [tool["tool_name"] for tool in metrics["tools"]] == ["read_page", "search_web"]  # calls desc
    read_page = tools["read_page"]
    assert read_page["calls"] == 4 and read_page["ok"] == 3 and read_page["failed"] == 1
    assert read_page["success_rate"] == 0.75
    assert read_page["cache_hits"] == 1 and read_page["cache_hit_rate"] == 0.25
    assert read_page["errors"] == {"upstream": 1}
    # sqlite -> python percentile fallback over done durations [100, 200, 300]
    assert read_page["p50_ms"] == 200.0
    assert read_page["p95_ms"] == 290.0
    assert tools["search_web"]["timeouts"] == 1
    assert tools["search_web"]["errors"] == {"timeout": 1}

    failures = metrics["recent_failures"]
    assert len(failures) == 2
    assert all(len(item["result_summary"]) <= 120 for item in failures)
    assert {item["error_class"] for item in failures} == {"upstream", "timeout"}


@pytest.mark.asyncio
async def test_user_isolation(session_factory):
    await seed(session_factory, [
        make_row("c1", "u1", "read_page", "done"),
        make_row("c2", "u2", "read_page", "failed", error_class="upstream"),
        make_row("c3", "u2", "search_web", "done"),
    ])
    async with session_factory() as session:
        metrics = await build_tool_metrics(session, user_id="u1", days=7)
    assert metrics["total_calls"] == 1
    assert metrics["tools"][0]["calls"] == 1
    assert metrics["recent_failures"] == []  # u2's failure must not leak


@pytest.mark.asyncio
async def test_window_excludes_old_rows(session_factory):
    await seed(session_factory, [
        make_row("old", "u1", "read_page", "done", created=NOW - timedelta(days=20)),
        make_row("new", "u1", "read_page", "done", created=NOW - timedelta(days=2)),
    ])
    async with session_factory() as session:
        metrics = await build_tool_metrics(session, user_id="u1", days=7)
    assert metrics["total_calls"] == 1


def test_resolve_window():
    assert resolve_window(1) == 1 and resolve_window(7) == 7 and resolve_window(30) == 30
    assert resolve_window(2) == 7 and resolve_window(365) == 7
    assert resolve_window(None) == 7 and resolve_window("abc") == 7


def test_percentile_linear_interpolation():
    assert percentile([], 0.5) is None
    assert percentile([100.0], 0.95) == 100.0
    assert percentile([100.0, 200.0, 300.0], 0.5) == 200.0
    assert percentile([100.0, 200.0, 300.0], 0.95) == 290.0
