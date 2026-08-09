from __future__ import annotations

from dataclasses import dataclass

from .deduplication import deduplicate_blocks
from .validation import SummaryValidation, validate_summary


@dataclass(frozen=True)
class CompactionResult:
    original_blocks: tuple[dict[str, object], ...]
    deduplicated_blocks: tuple[dict[str, object], ...]
    summary: dict[str, object]
    validation: SummaryValidation


def compact_reversibly(blocks: list[dict[str, object]], summary: dict[str, object] | None = None) -> CompactionResult:
    original = tuple(dict(block) for block in blocks)
    deduplicated = tuple(deduplicate_blocks(blocks))
    source_ids = {str(item.get("id", "")) for item in blocks if item.get("id")}
    safe_summary = summary or {
        "facts": [], "decisions": [], "constraints": [], "characters": [],
        "timeline": [], "open_questions": [], "unresolved_plot_threads": [],
        "style_requirements": [], "source_message_ids": sorted(source_ids),
    }
    validation = validate_summary(safe_summary, source_ids)
    return CompactionResult(original, deduplicated, safe_summary, validation)
