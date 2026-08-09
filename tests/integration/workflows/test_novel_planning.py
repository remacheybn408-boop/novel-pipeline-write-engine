import pytest

from proseforge.application.writing.planning_service import (
    ChapterPlanInput,
    PlanningService,
)


def plan(no: int) -> ChapterPlanInput:
    return ChapterPlanInput(no, 1, f"C{no}", "normal", "goal", "event", "conflict", ("Mira",), (), (), "ending", 3000)


def test_planning_rejects_missing_chapter():
    with pytest.raises(ValueError):
        PlanningService().validate([plan(1), plan(3)], expected_chapters=3, volumes=1, known_characters={"Mira"})


def test_planning_accepts_complete_coverage():
    assert len(PlanningService().validate([plan(1), plan(2)], expected_chapters=2, volumes=1, known_characters={"Mira"})) == 2
