from proseforge.context_engine.budgeting import calculate_budget


def test_budget_reserves_output_provider_and_margin():
    budget = calculate_budget(100_000, 10_000, 2_000, 0.10)
    assert budget.input_tokens == 78_000
