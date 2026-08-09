from proseforge.application.agents.review_swarm import (
    Finding,
    detect_conflicts,
    review_supported,
)


def test_review_conflicts_are_retained_and_unsupported_findings_flagged():
    left = Finding("a", "keep", "line-1"); right = Finding("b", "remove", "line-1")
    assert detect_conflicts([left, right]) == [(left, right)]; assert not review_supported(Finding("a", "unsupported", None))
