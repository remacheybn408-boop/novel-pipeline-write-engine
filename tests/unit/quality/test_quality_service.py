from proseforge.application.quality.service import QualityService


def test_quality_service_blocks_when_analyzer_fails():
    service = QualityService({"rule": lambda *args: {"status": "FAIL", "score": 90, "findings": [{"message": "bad"}]}})
    report = service.run("text")
    assert report.status == "BLOCK"
    assert report.analyzers == ("rule",)
