from proseforge.context_engine.compaction import compact_reversibly


def test_compaction_preserves_original_blocks_and_source_references():
    blocks = [{"id": "m1", "content": "A fact", "pinned": True}, {"id": "m2", "content": "  a   FACT  "}]
    result = compact_reversibly(blocks)

    assert result.original_blocks == tuple(blocks)
    assert len(result.deduplicated_blocks) == 1
    assert result.deduplicated_blocks[0]["source_ids"] == ["m1", "m2"]
    assert result.validation.status == "PASS"


def test_invalid_summary_blocks_compaction_validation():
    result = compact_reversibly([{"id": "m1", "content": "fact"}], {"facts": [], "source_message_ids": ["missing"]})

    assert result.validation.status == "BLOCK"
    assert "unknown_source_message" in result.validation.errors
