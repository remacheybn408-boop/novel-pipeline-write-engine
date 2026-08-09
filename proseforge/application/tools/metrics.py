"""Tool metrics aggregation for GET /api/v1/tools/metrics.

Single-user system, small tool_call_log: direct SQL aggregation per request
(no pre-aggregation table, no beat). Percentiles use PostgreSQL's
percentile_cont when available and fall back to an equivalent
linear-interpolation computation in Python on other dialects (keeps sqlite
test runs honest).

status enum (confirmed from orchestrator writes): "done" / "failed".
Timeouts are counted by error_class == "timeout", regardless of status.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from proseforge.infrastructure.database.models.tool_call import ToolCallLogModel

ALLOWED_WINDOWS = (1, 7, 30)
DEFAULT_WINDOW = 7
RECENT_FAILURES_LIMIT = 10
FAILURE_SUMMARY_CHARS = 120


def resolve_window(days: int | str | None) -> int:
    """Only 1/7/30 are valid; anything else falls back to the default."""
    try:
        value = int(days) if days is not None else DEFAULT_WINDOW
    except (TypeError, ValueError):
        return DEFAULT_WINDOW
    return value if value in ALLOWED_WINDOWS else DEFAULT_WINDOW


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolation percentile (same rule as percentile_cont)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = fraction * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _rate(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def assemble_metrics(*, window_days: int, since: datetime, tool_rows: list[dict], percentiles: dict[str, tuple[float | None, float | None]], failure_rows: list[dict]) -> dict:
    """Pure assembly: rows -> the exact response shape (empty-safe)."""
    tools: list[dict] = []
    for row in sorted(tool_rows, key=lambda item: item["calls"], reverse=True):
        calls = int(row["calls"])
        tools.append({
            "tool_name": row["tool_name"],
            "calls": calls,
            "ok": int(row["ok"]),
            "failed": int(row["failed"]),
            "success_rate": _rate(row["ok"], calls),
            "timeouts": int(row["timeouts"]),
            "cache_hits": int(row["cache_hits"]),
            "cache_hit_rate": _rate(row["cache_hits"], calls),
            "p50_ms": percentiles.get(row["tool_name"], (None, None))[0],
            "p95_ms": percentiles.get(row["tool_name"], (None, None))[1],
            "errors": row.get("errors") or {},
        })
    total_calls = sum(int(row["calls"]) for row in tool_rows)
    total_ok = sum(int(row["ok"]) for row in tool_rows)
    total_timeouts = sum(int(row["timeouts"]) for row in tool_rows)
    total_cache_hits = sum(int(row["cache_hits"]) for row in tool_rows)
    return {
        "window_days": window_days,
        "since": since.isoformat(),
        "total_calls": total_calls,
        "success_rate": _rate(total_ok, total_calls),
        "timeout_rate": _rate(total_timeouts, total_calls),
        "cache_hit_rate": _rate(total_cache_hits, total_calls),
        "tools": tools,
        "recent_failures": [
            {
                "tool_name": row["tool_name"],
                "error_class": row["error_class"],
                "result_summary": str(row["result_summary"] or "")[:FAILURE_SUMMARY_CHARS],
                "created_at": row["created_at"].isoformat() if isinstance(row["created_at"], datetime) else str(row["created_at"]),
            }
            for row in failure_rows
        ],
    }


async def build_tool_metrics(session, *, user_id: str, days: int) -> dict:
    """Run the aggregation queries for ONE user and assemble the response."""
    window = resolve_window(days)
    since = datetime.now(UTC) - timedelta(days=window)
    scope = [
        ToolCallLogModel.user_id == user_id,
        ToolCallLogModel.created_at >= since,
    ]

    agg_result = await session.execute(
        select(
            ToolCallLogModel.tool_name,
            func.count().label("calls"),
            func.count().filter(ToolCallLogModel.status == "done").label("ok"),
            func.count().filter(ToolCallLogModel.status == "failed").label("failed"),
            func.count().filter(ToolCallLogModel.error_class == "timeout").label("timeouts"),
            func.count().filter(ToolCallLogModel.cache_hit.is_(True)).label("cache_hits"),
        )
        .where(*scope)
        .group_by(ToolCallLogModel.tool_name)
    )
    tool_rows = [
        {"tool_name": name, "calls": calls, "ok": ok, "failed": failed, "timeouts": timeouts, "cache_hits": cache_hits, "errors": {}}
        for name, calls, ok, failed, timeouts, cache_hits in agg_result
    ]

    error_result = await session.execute(
        select(ToolCallLogModel.tool_name, ToolCallLogModel.error_class, func.count())
        .where(*scope, ToolCallLogModel.status == "failed", ToolCallLogModel.error_class.is_not(None))
        .group_by(ToolCallLogModel.tool_name, ToolCallLogModel.error_class)
    )
    errors_by_tool: dict[str, dict[str, int]] = {}
    for name, error_class, count in error_result:
        errors_by_tool.setdefault(name, {})[error_class] = int(count)
    for row in tool_rows:
        row["errors"] = errors_by_tool.get(row["tool_name"], {})

    percentiles = await _percentiles(session, scope)

    failure_result = await session.execute(
        select(ToolCallLogModel.tool_name, ToolCallLogModel.error_class, ToolCallLogModel.result_summary, ToolCallLogModel.created_at)
        .where(*scope, ToolCallLogModel.status == "failed")
        .order_by(ToolCallLogModel.created_at.desc())
        .limit(RECENT_FAILURES_LIMIT)
    )
    failure_rows = [
        {"tool_name": name, "error_class": error_class, "result_summary": summary, "created_at": created_at}
        for name, error_class, summary, created_at in failure_result
    ]

    return assemble_metrics(window_days=window, since=since, tool_rows=tool_rows, percentiles=percentiles, failure_rows=failure_rows)


async def _percentiles(session, scope: list) -> dict[str, tuple[float | None, float | None]]:
    """p50/p95 of duration_ms over done rows, per tool."""
    done_scope = [*scope, ToolCallLogModel.status == "done"]
    bind = session.bind
    if bind is not None and bind.dialect.name == "postgresql":
        result = await session.execute(
            select(
                ToolCallLogModel.tool_name,
                func.percentile_cont(0.5).within_group(ToolCallLogModel.duration_ms),
                func.percentile_cont(0.95).within_group(ToolCallLogModel.duration_ms),
            )
            .where(*done_scope)
            .group_by(ToolCallLogModel.tool_name)
        )
        return {name: (_round(p50), _round(p95)) for name, p50, p95 in result}
    # Other dialects (sqlite in tests): durations are small in number, compute in Python.
    result = await session.execute(
        select(ToolCallLogModel.tool_name, ToolCallLogModel.duration_ms).where(*done_scope)
    )
    durations: dict[str, list[float]] = {}
    for name, duration in result:
        durations.setdefault(name, []).append(float(duration))
    return {name: (_round(percentile(values, 0.5)), _round(percentile(values, 0.95))) for name, values in durations.items()}


def _round(value: float | None) -> float | None:
    return round(float(value), 1) if value is not None else None
