from proseforge.application.conversations.terminal_state import terminal_message_status


def test_generation_without_output_is_failed():
    assert terminal_message_status(0) == "FAILED"


def test_generation_with_output_is_partial():
    assert terminal_message_status(2) == "PARTIAL"
