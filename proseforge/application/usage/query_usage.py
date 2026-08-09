from __future__ import annotations

from datetime import UTC, datetime, timedelta

from proseforge.application.tools.metrics import resolve_window

_SUM_FIELDS = ("calls", "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens")


def aggregate_usage(records) -> dict[str, dict[str, int | float | None]]:
    buckets = {
        "actual": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "cost_usd": None},
        "estimated": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "cost_usd": None},
    }
    for row in records:
        bucket = buckets["actual" if row.usage_source == "provider" else "estimated"]
        for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens"):
            bucket[field] += int(getattr(row, field, 0) or 0)
        if row.cost_usd is not None:
            bucket["cost_usd"] = float(bucket["cost_usd"] or 0) + float(row.cost_usd)
    return buckets


def aggregate_by_model(rows, *, days: int) -> dict[str, object]:
    """Pure assembly: GROUP BY rows -> {days, rows, totals} (empty-safe).

    Rows come from the repository's per-(provider, model_id) aggregation.
    Totals roll every model up; avg_latency_ms is call-weighted.
    """
    models: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda item: int(item["total_tokens"] or 0), reverse=True):
        cost = row["cost_usd"]
        avg_latency = row["avg_latency_ms"]
        last_used = row["last_used_at"]
        models.append({
            "provider": row["provider"],
            "model_id": row["model_id"],
            "calls": int(row["calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cost_usd": round(float(cost), 6) if cost is not None else None,
            "avg_latency_ms": round(float(avg_latency), 1) if avg_latency is not None else None,
            "last_used_at": last_used.isoformat() if isinstance(last_used, datetime) else str(last_used),
        })
    totals: dict[str, object] = {field: sum(int(model[field]) for model in models) for field in _SUM_FIELDS}
    costs = [float(model["cost_usd"]) for model in models if model["cost_usd"] is not None]
    totals["cost_usd"] = round(sum(costs), 6) if costs else None
    weighted = [(float(model["avg_latency_ms"]), int(model["calls"])) for model in models if model["avg_latency_ms"] is not None]
    totals["avg_latency_ms"] = round(sum(avg * calls for avg, calls in weighted) / sum(calls for _, calls in weighted), 1) if weighted else None
    return {"days": days, "rows": models, "totals": totals}


class UsageQuery:
    def __init__(self, repository):
        self.repository = repository

    async def records(self, user_id: str, **filters):
        return await self.repository.list_for_user(user_id, **filters)

    async def summary(self, user_id: str, **filters):
        # A summary must not inherit the paginated records endpoint default.
        filters.pop("limit", None)
        rows = await self.repository.list_all_for_user(user_id, **filters)
        return aggregate_usage(rows)

    async def by_model(self, user_id: str, *, days: int | str | None = None):
        """Per-(provider, model_id) aggregation over a 1/7/30-day window."""
        window = resolve_window(days)
        since = datetime.now(UTC) - timedelta(days=window)
        rows = await self.repository.aggregate_by_model(user_id, since=since)
        return aggregate_by_model(rows, days=window)
