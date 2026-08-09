from proseforge.application.conversations.compare_branches import compare_messages


class Item:
    def __init__(self, id, role="assistant", content="", generation_attempt=1, parent_message_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.generation_attempt = generation_attempt
        self.parent_message_id = parent_message_id


def test_compare_keeps_branch_candidates_separate():
    result = compare_messages([Item("a"), Item("left")], [Item("a"), Item("right")])
    assert result["common_count"] == 1
    assert result["left"][0]["id"] == "left"
    assert result["right"][0]["id"] == "right"


def test_compare_returns_message_level_diff_entries():
    left = [Item("m1", "user", "q"), Item("m2", "assistant", "draft one", 1, "m1"), Item("m3", "assistant", "draft two", 2, "m1")]
    right = [Item("m1", "user", "q"), Item("m2", "assistant", "draft one", 1, "m1"), Item("m4", "user", "follow up")]
    result = compare_messages(left, right)
    assert result["common_count"] == 2
    assert result["left"] == [{"id": "m3", "role": "assistant", "content": "draft two", "generation_attempt": 2, "parent_message_id": "m1"}]
    assert result["right"] == [{"id": "m4", "role": "user", "content": "follow up", "generation_attempt": 1, "parent_message_id": None}]


def test_compare_identical_branches_have_empty_tails():
    result = compare_messages([Item("a"), Item("b")], [Item("a"), Item("b")])
    assert result == {"common_count": 2, "left": [], "right": []}


def test_compare_does_not_mutate_inputs():
    left = [Item("a"), Item("b")]
    right = [Item("a")]
    compare_messages(left, right)
    assert [item.id for item in left] == ["a", "b"]
    assert [item.id for item in right] == ["a"]
