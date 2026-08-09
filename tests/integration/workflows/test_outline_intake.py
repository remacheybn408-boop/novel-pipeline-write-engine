from proseforge.application.outlines.intake_service import OutlineIntakeService


def test_outline_intake_asks_only_for_missing_plan_details():
    service = OutlineIntakeService()
    spec = service.parse({"title": "T", "genre": "mystery", "characters": ["Mira"], "point_of_view": "third"})
    questions = service.clarification_questions(spec)
    assert questions == ("计划写多少卷？每卷多少章，或全书总章节数？", "单章大约多少字？")
