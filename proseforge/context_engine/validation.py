from __future__ import annotations

from dataclasses import dataclass

SUMMARY_FIELDS = (
    "facts", "decisions", "constraints", "characters", "timeline",
    "open_questions", "unresolved_plot_threads", "style_requirements",
    "source_message_ids",
)


@dataclass(frozen=True)
class SummaryValidation:
    status: str
    errors: tuple[str, ...]


def validate_summary(summary: dict[str, object], source_ids: set[str]) -> SummaryValidation:
    errors: list[str] = []
    for field in SUMMARY_FIELDS:
        if field not in summary or not isinstance(summary[field], list):
            errors.append(f"missing_or_invalid:{field}")
    referenced = {str(item) for item in summary.get("source_message_ids", [])}
    if not referenced.issubset(source_ids):
        errors.append("unknown_source_message")
    return SummaryValidation("BLOCK" if errors else "PASS", tuple(errors))
