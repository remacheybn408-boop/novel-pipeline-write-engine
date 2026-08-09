from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityReport:
    status: str
    score: float
    findings: tuple[dict[str, object], ...]
    analyzers: tuple[str, ...]


class QualityService:
    def __init__(self, analyzers: dict[str, Callable[..., dict]]):
        self.analyzers = analyzers

    def run(self, content: str, chapter_no: int = 0, context: dict | None = None) -> QualityReport:
        reports = [fn(content, chapter_no, context or {}) for fn in self.analyzers.values()]
        findings = tuple(item for report in reports for item in report.get("findings", []))
        scores = [float(report.get("score", 0)) for report in reports]
        score = sum(scores) / len(scores) if scores else 0.0
        status = "BLOCK" if any(str(report.get("status", "")).upper() in {"FAIL", "BLOCK"} for report in reports) else ("WARN" if findings else "PASS")
        return QualityReport(status, score, findings, tuple(self.analyzers))
