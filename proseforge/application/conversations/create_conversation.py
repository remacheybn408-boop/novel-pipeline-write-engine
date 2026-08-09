from __future__ import annotations

from proseforge.domain.conversation.entity import Conversation


class CreateConversation:
    def __init__(self, uow_factory):
        self.uow_factory = uow_factory

    async def execute(self, *, project_id: str, title: str = "Untitled conversation"):
        conversation = Conversation.create(project_id, title)
        async with self.uow_factory() as uow:
            branch = await uow.conversations.create(conversation)
            await uow.commit()
        return conversation, branch
