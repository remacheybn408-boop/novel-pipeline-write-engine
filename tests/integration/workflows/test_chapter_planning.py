from proseforge.application.planning.chapter_planner import plan_chapters


def test_chapter_planner_has_deterministic_numbering():
    plans = plan_chapters(volumes=2, chapters_per_volume=2, word_target=3000)
    assert [item.chapter_no for item in plans] == [1, 2, 3, 4]
    assert plans[-1].word_target == 3000
