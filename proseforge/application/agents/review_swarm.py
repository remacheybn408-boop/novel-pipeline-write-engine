from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    reviewer: str
    message: str
    evidence: str | None
    severity: str = "suggestion"

def detect_conflicts(findings: list[Finding]) -> list[tuple[Finding, Finding]]:
    return [(left, right) for index, left in enumerate(findings) for right in findings[index + 1:] if left.evidence and left.evidence == right.evidence and left.message != right.message]

def review_supported(finding: Finding) -> bool: return bool(finding.evidence)
