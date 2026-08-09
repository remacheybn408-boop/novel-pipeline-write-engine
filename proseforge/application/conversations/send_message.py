from __future__ import annotations

from collections.abc import Iterable

from proseforge.application.files.message_attachments import link_attachments_to_message


class SendMessage:
    def __init__(self, uow_factory, queue, event_stream=None):
        self.uow_factory = uow_factory
        self.queue = queue
        self.event_stream = event_stream

    async def execute(self, *, branch_id: str, content: str, client_request_id: str, user_id: str = "", provider: str = "openai", model: str = "gpt-4.1-mini", reasoning_level: str = "auto", attachment_ids: Iterable[str] = ()):
        async with self.uow_factory() as uow:
            lock = getattr(uow.conversations, "lock_client_request", None)
            if lock is not None:
                await lock(client_request_id, user_id)
            lookup = getattr(uow.conversations, "get_by_client_request_id", None)
            existing = await lookup(client_request_id, user_id) if lookup is not None else None
            if existing is not None:
                assistant_lookup = getattr(uow.conversations, "assistant_after", None)
                assistant = await assistant_lookup(existing.id) if assistant_lookup is not None else None
                if assistant is not None:
                    return existing, assistant, "deduplicated"
            user = await uow.conversations.append_message(branch_id, "user", content, client_request_id, "COMPLETED", user_id=user_id)
            # Link pre-uploaded attachments in the same transaction, before
            # the generation task can read history (no unlink race).
            if attachment_ids:
                await link_attachments_to_message(uow.session, attachment_ids, user.id)
            assistant = await uow.conversations.append_message(branch_id, "assistant", "", None, "PENDING", parent_message_id=user.id)
            await uow.commit()
        try:
            task_id = await self.queue.enqueue("proseforge.chat.generate", {"message_id": assistant.id, "user_message_id": user.id, "user_id": user_id, "provider": provider, "model": model, "reasoning_level": reasoning_level})
        except Exception:
            # Enqueue failed after the commit above: the user message and the
            # PENDING assistant placeholder are already persisted with no task
            # and no event. Flip the placeholder to FAILED in a fresh
            # transaction so it never strands as a PENDING orphan.
            async with self.uow_factory() as uow:
                await uow.conversations.set_message_status(assistant.id, "FAILED")
                conversation_lookup = getattr(uow.conversations, "conversation_id_for_message", None)
                conversation_id = await conversation_lookup(assistant.id) if conversation_lookup else None
                await uow.commit()
            if self.event_stream is not None:
                # SSE live tails end on the terminal message.failed event.
                failed_payload = {"event": "message.failed", "message_id": assistant.id, "status": "FAILED", "reason": "queue-unavailable"}
                await self.event_stream.publish(f"message:{assistant.id}", failed_payload)
                if conversation_id:
                    await self.event_stream.publish(f"conversation:{conversation_id}", failed_payload)
            raise
        return user, assistant, task_id
