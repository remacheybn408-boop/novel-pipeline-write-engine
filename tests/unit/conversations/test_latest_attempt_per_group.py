"""latest_attempt_per_group：模型上下文组装只保留每个 regenerate 组的最新候选。

M9 回归：regenerate 在同分支 append 候选而不 fork，旧候选若随
list_visible_messages 全量进入上下文会自我污染。展示路径不走此过滤，
仍返回全部候选（前端按 generation_attempt 分组渲染）。
"""

from __future__ import annotations

from proseforge.domain.conversation.candidates import latest_attempt_per_group
from proseforge.domain.conversation.entity import Message


def _message(message_id: str, role: str, parent_message_id: str | None = None, generation_attempt: int = 1) -> Message:
    return Message(id=message_id, branch_id="b1", role=role, content=message_id, parent_message_id=parent_message_id, generation_attempt=generation_attempt)


def test_regenerate_twice_keeps_only_the_latest_candidate():
    history = [
        _message("u1", "user"),
        _message("a1", "assistant", parent_message_id="u1", generation_attempt=1),
        _message("a2", "assistant", parent_message_id="u1", generation_attempt=2),
        _message("a3", "assistant", parent_message_id="u1", generation_attempt=3),
    ]

    visible = latest_attempt_per_group(history)

    assert [message.id for message in visible] == ["u1", "a3"]


def test_surviving_candidate_stays_at_its_own_position():
    history = [
        _message("u1", "user"),
        _message("a1", "assistant", parent_message_id="u1", generation_attempt=1),
        _message("u2", "user"),
        _message("a2", "assistant", parent_message_id="u1", generation_attempt=2),
    ]

    visible = latest_attempt_per_group(history)

    assert [message.id for message in visible] == ["u1", "u2", "a2"]


def test_independent_groups_each_keep_their_latest_candidate():
    history = [
        _message("u1", "user"),
        _message("a1", "assistant", parent_message_id="u1", generation_attempt=1),
        _message("a2", "assistant", parent_message_id="u1", generation_attempt=2),
        _message("u2", "user"),
        _message("b1", "assistant", parent_message_id="u2", generation_attempt=1),
        _message("b2", "assistant", parent_message_id="u2", generation_attempt=2),
    ]

    visible = latest_attempt_per_group(history)

    assert [message.id for message in visible] == ["u1", "a2", "u2", "b2"]


def test_legacy_parentless_source_groups_with_its_candidates():
    # 退化路径：源消息无 parent 时 regenerate 候选挂在源消息自身 id 上，
    # 源消息（parent=None → 自身 id 为 key）与候选同属一组。
    history = [
        _message("a1", "assistant", parent_message_id=None, generation_attempt=1),
        _message("a2", "assistant", parent_message_id="a1", generation_attempt=2),
    ]

    visible = latest_attempt_per_group(history)

    assert [message.id for message in visible] == ["a2"]


def test_plain_history_without_candidates_is_unchanged():
    history = [
        _message("u1", "user"),
        _message("a1", "assistant", parent_message_id="u1", generation_attempt=1),
        _message("u2", "user"),
        _message("a2", "assistant", parent_message_id="u2", generation_attempt=1),
    ]

    assert latest_attempt_per_group(history) == history
