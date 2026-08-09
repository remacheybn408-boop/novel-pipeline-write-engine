class ForkBranch:
    def __init__(self, uow_factory):
        self.uow_factory = uow_factory

    async def execute(self, *, conversation_id: str, message_id: str, name: str):
        async with self.uow_factory() as uow:
            branch = await uow.conversations.fork(conversation_id, message_id, name)
            await uow.commit()
            return branch
