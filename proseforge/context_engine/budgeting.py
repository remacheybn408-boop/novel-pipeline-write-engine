from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBudget:
    context_window: int
    input_tokens: int
    output_reserve: int
    provider_reserved: int


def calculate_budget(context_window: int, requested_output: int, provider_reserved: int = 0, safety_margin_ratio: float = 0.1) -> ContextBudget:
    margin = int(context_window * safety_margin_ratio)
    return ContextBudget(context_window, max(0, context_window - requested_output - provider_reserved - margin), requested_output, provider_reserved)
