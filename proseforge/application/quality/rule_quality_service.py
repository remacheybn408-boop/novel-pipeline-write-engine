from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityDecision:
    status: str
    can_commit: bool
    blocked_by: tuple[str, ...]
    warnings: tuple[dict[str, object], ...]
    report: dict[str, object]


class RuleQualityService:
    def __init__(self, guards: dict[str, object], escalate_warnings: bool = False):
        self.guards = guards
        self.escalate_warnings = escalate_warnings

    def run(self, content: str, chapter_no: int = 0, context: dict | None = None) -> QualityDecision:
        blocked: list[str] = []
        warnings: list[dict[str, object]] = []
        report: dict[str, object] = {}
        for name, guard in self.guards.items():
            try:
                result = guard(content, chapter_no, context or {}) if callable(guard) else guard.run(content, chapter_no, context or {})
            except Exception as exc:
                report[name] = {"status": "ERROR", "error": type(exc).__name__}
                blocked.append(name)
                continue
            report[name] = result
            status = str(result.get("status", "WARN")).upper()
            if status in {"FAIL", "BLOCK", "ERROR"}:
                blocked.append(name)
            elif status in {"WARN", "WARNING"}:
                warnings.append({"guard": name, **result})
        status = "BLOCK" if blocked else ("BLOCK" if self.escalate_warnings and warnings else ("WARN" if warnings else "PASS"))
        return QualityDecision(status, not blocked and not (self.escalate_warnings and warnings), tuple(blocked), tuple(warnings), report)
