from proseforge.application.revision.approve_proposal import apply_hunks


def test_apply_hunks_uses_reverse_offsets_for_multiple_accepted_changes():
    assert apply_hunks("one two three", [{"start": 0, "end": 3, "replacement": "ONE"}, {"start": 4, "end": 7, "replacement": "TWO"}]) == "ONE TWO three"
