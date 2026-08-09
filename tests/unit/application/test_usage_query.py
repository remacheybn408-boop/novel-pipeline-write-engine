from datetime import UTC, datetime
from types import SimpleNamespace

from proseforge.application.usage.query_usage import aggregate_by_model, aggregate_usage


def test_usage_summary_keeps_actual_and_estimated_buckets_separate():
    result = aggregate_usage([
        SimpleNamespace(usage_source="provider", input_tokens=4, output_tokens=3, cached_input_tokens=0, reasoning_tokens=0, total_tokens=7, cost_usd=None),
        SimpleNamespace(usage_source="estimated", input_tokens=2, output_tokens=1, cached_input_tokens=0, reasoning_tokens=0, total_tokens=3, cost_usd=None),
    ])

    assert result["actual"]["total_tokens"] == 7
    assert result["estimated"]["total_tokens"] == 3
    assert result["actual"]["cost_usd"] is None


def _group_row(provider, model_id, *, calls, total_tokens, cost_usd=None, avg_latency_ms=None, last_used_at=None, input_tokens=0, output_tokens=0, cached_input_tokens=0, reasoning_tokens=0):
    return {
        "provider": provider, "model_id": model_id, "calls": calls,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens, "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens, "cost_usd": cost_usd,
        "avg_latency_ms": avg_latency_ms,
        "last_used_at": last_used_at or datetime(2026, 8, 8, tzinfo=UTC),
    }


def test_aggregate_by_model_empty_rows_are_safe():
    result = aggregate_by_model([], days=7)

    assert result["days"] == 7
    assert result["rows"] == []
    assert result["totals"]["calls"] == 0 and result["totals"]["total_tokens"] == 0
    assert result["totals"]["cost_usd"] is None and result["totals"]["avg_latency_ms"] is None


def test_aggregate_by_model_sorts_by_tokens_and_rolls_up_totals():
    result = aggregate_by_model([
        _group_row("openai", "gpt-4o", calls=2, total_tokens=100, cost_usd=0.01, avg_latency_ms=100.0),
        _group_row("deepseek", "deepseek-chat", calls=1, total_tokens=300, cost_usd=None, avg_latency_ms=200.0),
    ], days=7)

    assert [row["model_id"] for row in result["rows"]] == ["deepseek-chat", "gpt-4o"]  # total_tokens desc
    totals = result["totals"]
    assert totals["calls"] == 3 and totals["total_tokens"] == 400
    assert totals["cost_usd"] == 0.01  # null cost of the other model is skipped
    assert totals["avg_latency_ms"] == round((200.0 * 1 + 100.0 * 2) / 3, 1)  # call-weighted


def test_aggregate_by_model_serializes_last_used_at():
    result = aggregate_by_model([_group_row("openai", "gpt-4o", calls=1, total_tokens=10, last_used_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC))], days=1)

    assert result["rows"][0]["last_used_at"] == "2026-08-08T12:00:00+00:00"
