from __future__ import annotations

import pytest

from proseforge.application.conversations.edit_message import EditMessage
from proseforge.domain.conversation.entity import ConversationBranch, Message


class Repo:
    def __init__(self):
        self.source = Message("source", "main", "user", "original")
        self.edits = []

    async def get_message(self, message_id): return self.source if message_id == "source" else None
    async def belongs_to_owner(self, conversation_id, user_id): return True
    async def fork_owned(self, conversation_id, message_id, name, user_id): return ConversationBranch("branch", conversation_id, name)
    async def append_message(self, branch_id, role, content, client_request_id, status, **kwargs): return Message("replacement", branch_id, role, content, status=status, parent_message_id=kwargs.get("parent_message_id"))
    async def create_message_edit(self, *args): self.edits.append(args)


class Uow:
    def __init__(self, repo): self.conversations = repo
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass


@pytest.mark.asyncio
async def test_edit_creates_branch_and_preserves_original():
    repo = Repo()
    result = await EditMessage(lambda: Uow(repo)).execute(conversation_id="c", message_id="source", content="edited", user_id="u")
    assert result.branch_id == "branch"
    assert result.replacement_message_id == "replacement"
    assert repo.source.content == "original"
    assert repo.edits == [("source", "original", "edited", "branch")]
