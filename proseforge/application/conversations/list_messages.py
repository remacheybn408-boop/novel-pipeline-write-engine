class ListVisibleMessages:
    def __init__(self, uow_factory):
        self.uow_factory = uow_factory

    async def execute(self, *, branch_id: str):
        async with self.uow_factory() as uow:
            return await uow.conversations.list_visible_messages(branch_id)
