from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EditMessageResult:
    branch_id: str
    source_message_id: str
    replacement_message_id: str


class EditMessage:
    def __init__(self, uow_factory):
        self.uow_factory = uow_factory

    async def execute(self, *, conversation_id: str, message_id: str, content: str, user_id: str) -> EditMessageResult:
        async with self.uow_factory() as uow:
            repo = uow.conversations
            source = await repo.get_message(message_id)
            if source is None or not await repo.belongs_to_owner(conversation_id, user_id):
                raise LookupError("message not found")
            branch = await repo.fork_owned(conversation_id, message_id, "Edited message", user_id)
            if branch is None:
                raise LookupError("message not found")
            replacement = await repo.append_message(branch.id, source.role, content, None, "COMPLETED", parent_message_id=message_id)
            await repo.create_message_edit(message_id, source.content, content, branch.id)
            await uow.commit()
            return EditMessageResult(branch.id, message_id, replacement.id)
