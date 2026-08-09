def terminal_message_status(chunk_count: int) -> str:
    return "PARTIAL" if chunk_count > 0 else "FAILED"
