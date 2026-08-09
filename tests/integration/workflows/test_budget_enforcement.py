from proseforge.domain.workflow.budget import budget_blocked


def test_token_budget_blocks_before_next_model_call():
    assert budget_blocked(used_tokens=90, token_limit=100, estimated_next_tokens=15, estimated_cost=0, cost_limit=0, estimated_next_cost=None)


def test_unknown_price_does_not_fake_a_zero_cost_budget_result():
    assert not budget_blocked(used_tokens=10, token_limit=100, estimated_next_tokens=10, estimated_cost=0, cost_limit=1, estimated_next_cost=None)
