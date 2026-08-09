from proseforge.domain.common.ids import new_id


def test_new_id_is_lexically_sortable_string() -> None:
    first = new_id()
    second = new_id()
    assert isinstance(first, str)
    assert len(first) >= 20
    assert first < second
