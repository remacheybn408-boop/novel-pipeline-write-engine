from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from proseforge.domain.model.known_max_output import lookup_max_output
from proseforge.domain.model.known_reasoning import ReasoningProfile, lookup_reasoning
from proseforge.domain.model.known_windows import lookup_context_window

# Input-assembly budget per agent task: 65% of the model's real window
# (capped by context_budget_cap() on the budgeting side). The remaining 35%
# covers system prompt overhead + max_output_tokens.
CONTEXT_INPUT_RATIO = 0.65

# Budgeting-side ceiling (NOT the display value): lane/task budgets never size
# against a window above this, so a bogus multi-million catalog entry cannot
# inflate budget ledgers. Override with PROSEFORGE_CONTEXT_BUDGET_CAP.
_DEFAULT_CONTEXT_BUDGET_CAP = 1_048_576


def context_budget_cap() -> int:
    """Hard ceiling applied where context windows size token budgets."""
    try:
        return int(os.environ.get("PROSEFORGE_CONTEXT_BUDGET_CAP", str(_DEFAULT_CONTEXT_BUDGET_CAP)))
    except ValueError:
        return _DEFAULT_CONTEXT_BUDGET_CAP


class ReasoningLevel(str, Enum):
    AUTO = "auto"
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int
    supports_reasoning: bool
    reasoning_parameter: str | None
    supports_tools: bool
    supports_vision: bool
    source: Literal["catalog", "provider", "user", "fallback"]
    reasoning_profile: ReasoningProfile | None = None


def capabilities_from_model(model) -> ModelCapabilities:
    raw = model.capabilities or {}
    provider = getattr(model, "provider", None)
    model_id = getattr(model, "model_id", None)
    # Verified known windows beat user-entered/catalog values: a manual entry
    # (e.g. 700K typed into the model form) must never inflate a model past
    # its documented window — clamping down is the point of the table.
    # Display uses the model's REAL window (no 700K product cap): the usage
    # ring and the models panel show what the endpoint actually serves.
    # Budget sizing applies its own ceiling via context_budget_cap().
    known_window = lookup_context_window(provider, model_id)
    context_window = known_window or int(model.context_window or raw.get("context_window") or 8192)
    # Catalog wins; the verified known table is the fallback for synced models (capabilities={}).
    reasoning_profile = None if raw.get("reasoning_parameter") else lookup_reasoning(provider, model_id)
    supports_reasoning = bool(raw.get("reasoning", False)) or reasoning_profile is not None
    # max_output_tokens precedence: catalog column > capabilities dict >
    # verified known table (known_max_output) > 8192 floor. The 8192 floor
    # replaced the old 1024: every mainstream model since GPT-4 allows >=8K
    # output (1024 let max reasoning burn the whole budget and produce empty
    # replies), and table misses are models we know nothing about anyway.
    max_output = int(model.max_output_tokens or raw.get("max_output_tokens") or lookup_max_output(provider, model_id) or 8192)
    return ModelCapabilities(int(context_window), max_output, supports_reasoning, str(raw.get("reasoning_parameter")) if raw.get("reasoning_parameter") else None, bool(raw.get("tools", False)), bool(raw.get("vision", False)), "catalog", reasoning_profile)
