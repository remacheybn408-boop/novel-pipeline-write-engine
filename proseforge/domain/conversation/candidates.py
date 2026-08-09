"""Regenerate candidate selection for model context assembly.

Regenerate appends assistant candidates on the same branch (sharing one
parent_message_id edge) instead of forking, so a naive visible-message list
contains every rejected attempt. Sending those stale attempts back to the
model is self-pollution: for context assembly each regenerate group must
contribute only its latest candidate. Display paths keep the full list and
group bubbles by generation_attempt instead.
"""

from __future__ import annotations

from proseforge.domain.conversation.entity import Message


def latest_attempt_per_group(messages: list[Message]) -> list[Message]:
    """Keep only the latest assistant candidate per regenerate group.

    Group key is the shared parent edge (parent_message_id); legacy messages
    without a parent fall back to their own id, which also groups the
    "hang on itself" degenerate regenerate (candidates point at a source
    that itself has no parent). User/system messages always pass through.
    Order is preserved: the surviving candidate stays at its own position.
    """
    latest_index: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.role != "assistant":
            continue
        key = message.parent_message_id or message.id
        current = latest_index.get(key)
        if current is None or (message.generation_attempt, index) > (messages[current].generation_attempt, current):
            latest_index[key] = index
    return [
        message
        for index, message in enumerate(messages)
        if message.role != "assistant" or latest_index[message.parent_message_id or message.id] == index
    ]
