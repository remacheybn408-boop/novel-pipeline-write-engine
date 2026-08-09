from __future__ import annotations


def budget_blocked(*, used_tokens: int, token_limit: int, estimated_next_tokens: int, estimated_cost: float, cost_limit: float, estimated_next_cost: float | None) -> bool:
    token_blocked = token_limit > 0 and used_tokens + max(0, estimated_next_tokens) > token_limit
    cost_blocked = cost_limit > 0 and estimated_next_cost is not None and estimated_cost + estimated_next_cost > cost_limit
    return token_blocked or cost_blocked
