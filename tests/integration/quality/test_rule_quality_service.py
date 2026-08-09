from proseforge.application.quality.rule_quality_service import RuleQualityService


def test_guard_crash_is_error_and_cannot_commit():
    decision = RuleQualityService({"broken": lambda *args: (_ for _ in ()).throw(RuntimeError())}).run("text")
    assert decision.status == "BLOCK"
    assert not decision.can_commit
    assert decision.report["broken"]["status"] == "ERROR"
