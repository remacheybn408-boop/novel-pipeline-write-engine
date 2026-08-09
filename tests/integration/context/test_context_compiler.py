from proseforge.context_engine.compiler import compile_context


def test_pinned_canon_survives_before_optional_references():
    result = compile_context("snap", [{"id": "canon", "content": "canon", "pinned": True}, {"id": "old", "content": "x" * 100}], 10)
    assert "canon" in result.source_ids
    assert "old" in result.excluded_ids
