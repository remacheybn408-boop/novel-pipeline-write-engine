from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _entry(message: object) -> dict[str, Any]:
    return {
        "id": getattr(message, "id", None),
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", ""),
        "generation_attempt": getattr(message, "generation_attempt", 1) or 1,
        "parent_message_id": getattr(message, "parent_message_id", None),
    }


def compare_messages(left: Sequence[object], right: Sequence[object]) -> dict[str, object]:
    """Return a stable message-level comparison without mutating either branch history.

    The common prefix is reported as a count; each divergent tail is returned as
    per-message entries so the frontend can render both sides side by side.
    """
    common = 0
    while common < min(len(left), len(right)) and getattr(left[common], "id", None) == getattr(right[common], "id", None):
        common += 1
    return {
        "common_count": common,
        "left": [_entry(message) for message in left[common:]],
        "right": [_entry(message) for message in right[common:]],
    }
