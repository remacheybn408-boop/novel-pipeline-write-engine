from __future__ import annotations

import json

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.common.ids import new_id
from proseforge.domain.conversation.entity import (
    Conversation,
    ConversationBranch,
    Message,
    MessageChunk,
)
from proseforge.infrastructure.database.dialect import capabilities_for_engine
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationEventModel,
    ConversationModel,
    MessageChunkModel,
    MessageEditModel,
    MessageModel,
)


class SqlAlchemyConversationRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_client_request(self, client_request_id: str, user_id: str = "") -> None:
        """Serialize concurrent retries for the same idempotency key in PostgreSQL.

        锁键带 user_id：client_request_id 的唯一域是 (user_id, client_request_id)，
        不同用户的同名 key 不应互相串行阻塞。
        SQLite 由数据库级写锁串行化写入者，复合唯一约束兜底。
        """
        if capabilities_for_engine(self.session.bind).supports_advisory_locks:
            await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"client:{user_id}:{client_request_id}"})

    async def lock_regenerate(self, parent_message_id: str) -> None:
        """Serialize concurrent regenerates for the same parent message in PostgreSQL.

        SQLite 由数据库级写锁串行化写入者。锁键带 regenerate: 前缀，
        避免与 lock_client_request 的锁域在 hashtext 下冲突。
        """
        if capabilities_for_engine(self.session.bind).supports_advisory_locks:
            await self.session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"), {"lock_key": f"regenerate:{parent_message_id}"})

    async def create(self, conversation: Conversation) -> ConversationBranch:
        self.session.add(ConversationModel(id=conversation.id, project_id=conversation.project_id, title=conversation.title, created_at=conversation.created_at))
        # Flush the parent first: the ORM does not order inserts by
        # table-level FK dependencies, and conversation_branches now has a
        # real FK to conversations (same reasoning as projects.add).
        await self.session.flush()
        branch = ConversationBranch(id=new_id(), conversation_id=conversation.id, name="Main")
        self.session.add(ConversationBranchModel(**branch.__dict__))
        await self.session.flush()
        return branch

    async def list_for_owner(self, owner_id: str, mode: str | None = None, project_id: str | None = None, archived: bool = False) -> list[Conversation]:
        """会话列表（归属校验经 projects.owner_id join），created_at 倒序，上限 100。
        archived=False（默认）只列未归档；True 只列已归档。"""
        from proseforge.infrastructure.database.models.project import ProjectModel

        query = (
            select(ConversationModel)
            .join(ProjectModel, ProjectModel.id == ConversationModel.project_id)
            .where(ProjectModel.owner_id == owner_id, ConversationModel.archived.is_(archived))
        )
        if mode is not None:
            query = query.where(ProjectModel.mode == mode)
        if project_id is not None:
            query = query.where(ConversationModel.project_id == project_id)
        rows = await self.session.scalars(query.order_by(ConversationModel.created_at.desc(), ConversationModel.id).limit(100))
        return [Conversation(id=row.id, project_id=row.project_id, title=row.title, created_at=row.created_at, archived=row.archived) for row in rows]

    async def set_archived(self, conversation_id: str, owner_id: str, archived: bool) -> bool:
        """Archive/unarchive a conversation owned by the user; False when not found/owned."""
        if not await self.belongs_to_owner(conversation_id, owner_id):
            return False
        row = await self.session.get(ConversationModel, conversation_id)
        if row is None:
            return False
        row.archived = archived
        await self.session.flush()
        return True

    async def append_message(self, branch_id: str, role: str, content: str, client_request_id: str | None = None, status: str = "COMPLETED", *, parent_message_id: str | None = None, generation_attempt: int = 1, user_id: str | None = None) -> Message:
        if client_request_id:
            # 幂等查重域与唯一约束一致：(user_id, client_request_id)。
            existing = await self.session.scalar(
                select(MessageModel).where(MessageModel.client_request_id == client_request_id, MessageModel.user_id == user_id)
            )
            if existing:
                return self._message(existing)
        next_sequence = (await self.session.scalar(select(func.coalesce(func.max(MessageModel.sequence_no), 0)).where(MessageModel.branch_id == branch_id))) + 1
        message = Message(id=new_id(), branch_id=branch_id, role=role, content=content, client_request_id=client_request_id, status=status, parent_message_id=parent_message_id, generation_attempt=generation_attempt)
        # model_snapshot/reasoning_snapshot 是领域字段；ORM 列是 *_json，由 set_message_snapshots 写入。
        data = {key: value for key, value in message.__dict__.items() if key not in {"model_snapshot", "reasoning_snapshot"}}
        self.session.add(MessageModel(**data, sequence_no=next_sequence, user_id=user_id))
        await self.session.flush()
        return message

    async def get_by_client_request_id(self, client_request_id: str, user_id: str | None = None) -> Message | None:
        query = select(MessageModel).where(MessageModel.client_request_id == client_request_id)
        if user_id is not None:
            query = query.where(MessageModel.user_id == user_id)
        row = await self.session.scalar(query)
        return self._message(row) if row else None

    async def assistant_after(self, user_message_id: str) -> Message | None:
        source = await self.session.get(MessageModel, user_message_id)
        if source is None:
            return None
        row = await self.session.scalar(
            select(MessageModel)
            .where(MessageModel.branch_id == source.branch_id, MessageModel.role == "assistant", MessageModel.sequence_no > source.sequence_no)
            .order_by(MessageModel.sequence_no)
        )
        return self._message(row) if row else None

    async def count_assistant_siblings(self, branch_id: str, parent_message_id: str) -> int:
        """Count assistant candidates sharing the same parent edge (regenerate siblings)."""
        return int(await self.session.scalar(
            select(func.count()).select_from(MessageModel).where(
                MessageModel.branch_id == branch_id,
                MessageModel.role == "assistant",
                MessageModel.parent_message_id == parent_message_id,
            )
        ) or 0)

    async def fork(self, conversation_id: str, forked_from_message_id: str, name: str) -> ConversationBranch:
        source = await self.session.scalar(select(MessageModel).where(MessageModel.id == forked_from_message_id))
        if source is None:
            raise ValueError("fork point message does not exist")
        branch = ConversationBranch(id=new_id(), conversation_id=conversation_id, name=name, parent_branch_id=source.branch_id, forked_from_message_id=forked_from_message_id)
        self.session.add(ConversationBranchModel(**branch.__dict__))
        await self.session.flush()
        return branch

    async def fork_owned(self, conversation_id: str, forked_from_message_id: str, name: str, owner_id: str) -> ConversationBranch | None:
        from proseforge.infrastructure.database.models.project import ProjectModel

        source = await self.session.scalar(
            select(MessageModel)
            .join(ConversationBranchModel, ConversationBranchModel.id == MessageModel.branch_id)
            .join(ConversationModel, ConversationModel.id == ConversationBranchModel.conversation_id)
            .join(ProjectModel, ProjectModel.id == ConversationModel.project_id)
            .where(
                MessageModel.id == forked_from_message_id,
                ConversationModel.id == conversation_id,
                ProjectModel.owner_id == owner_id,
            )
        )
        if source is None:
            return None
        branch = ConversationBranch(id=new_id(), conversation_id=conversation_id, name=name, parent_branch_id=source.branch_id, forked_from_message_id=forked_from_message_id)
        self.session.add(ConversationBranchModel(**branch.__dict__))
        await self.session.flush()
        return branch

    async def create_message_edit(self, message_id: str, original_content: str, edited_content: str, branch_id: str) -> None:
        from datetime import UTC, datetime

        from proseforge.infrastructure.database.models.conversation import (
            MessageEditModel,
        )
        self.session.add(MessageEditModel(id=new_id(), message_id=message_id, original_content=original_content, edited_content=edited_content, created_branch_id=branch_id, created_at=datetime.now(UTC)))
        await self.session.flush()

    async def list_visible_messages(self, branch_id: str) -> list[Message]:
        branch = await self.session.get(ConversationBranchModel, branch_id)
        if branch is None:
            return []
        own_rows = list((await self.session.scalars(select(MessageModel).where(MessageModel.branch_id == branch_id).order_by(MessageModel.sequence_no, MessageModel.id))).all())
        own = [self._message(item) for item in own_rows]
        if branch.parent_branch_id is None:
            return own
        ancestors = await self.list_visible_messages(branch.parent_branch_id)
        if branch.forked_from_message_id:
            ids = [item.id for item in ancestors]
            if branch.forked_from_message_id not in ids:
                raise ValueError("fork point is not visible from parent branch")
            ancestors = ancestors[: ids.index(branch.forked_from_message_id) + 1]
        return ancestors + own

    async def append_chunk(self, message_id: str, chunk_index: int, event_type: str, content: str) -> MessageChunk:
        existing = await self.session.scalar(select(MessageChunkModel).where(MessageChunkModel.message_id == message_id, MessageChunkModel.chunk_index == chunk_index))
        if existing:
            return self._chunk(existing)
        chunk = MessageChunk(id=new_id(), message_id=message_id, chunk_index=chunk_index, event_type=event_type, content=content)
        self.session.add(MessageChunkModel(**chunk.__dict__))
        message = await self.session.get(MessageModel, message_id)
        if message is None:
            raise ValueError("message does not exist")
        message.content = f"{message.content}{content}"
        await self.session.flush()
        return chunk

    async def chunk_count(self, message_id: str) -> int:
        return int(await self.session.scalar(select(func.count()).select_from(MessageChunkModel).where(MessageChunkModel.message_id == message_id)) or 0)

    async def get_message(self, message_id: str) -> Message | None:
        row = await self.session.get(MessageModel, message_id)
        return self._message(row) if row else None

    async def claim_generation(self, message_id: str) -> bool:
        """Atomically turn a dispatchable response into STREAMING once."""
        result = await self.session.execute(
            update(MessageModel)
            .where(MessageModel.id == message_id, MessageModel.status.in_(("PENDING", "PARTIAL")))
            .values(status="STREAMING")
        )
        return result.rowcount == 1

    async def conversation_id_for_message(self, message_id: str) -> str | None:
        return await self.session.scalar(
            select(ConversationModel.id)
            .join(ConversationBranchModel, ConversationBranchModel.conversation_id == ConversationModel.id)
            .join(MessageModel, MessageModel.branch_id == ConversationBranchModel.id)
            .where(MessageModel.id == message_id)
        )

    async def belongs_to_owner(self, conversation_id: str, owner_id: str) -> bool:
        from proseforge.infrastructure.database.models.project import ProjectModel
        row = await self.session.scalar(
            select(ConversationModel.id)
            .join(ProjectModel, ProjectModel.id == ConversationModel.project_id)
            .where(ConversationModel.id == conversation_id, ProjectModel.owner_id == owner_id)
        )
        return row is not None

    async def branch_belongs_to_conversation(self, branch_id: str, conversation_id: str, owner_id: str) -> bool:
        from proseforge.infrastructure.database.models.project import ProjectModel
        row = await self.session.scalar(
            select(ConversationBranchModel.id)
            .join(ConversationModel, ConversationModel.id == ConversationBranchModel.conversation_id)
            .join(ProjectModel, ProjectModel.id == ConversationModel.project_id)
            .where(
                ConversationBranchModel.id == branch_id,
                ConversationBranchModel.conversation_id == conversation_id,
                ProjectModel.owner_id == owner_id,
            )
        )
        return row is not None

    async def list_branches(self, conversation_id: str, owner_id: str, include_archived: bool = False) -> list[ConversationBranch]:
        from proseforge.infrastructure.database.models.project import ProjectModel
        query = (
            select(ConversationBranchModel)
            .join(ConversationModel, ConversationModel.id == ConversationBranchModel.conversation_id)
            .join(ProjectModel, ProjectModel.id == ConversationModel.project_id)
            .where(ConversationBranchModel.conversation_id == conversation_id, ProjectModel.owner_id == owner_id)
        )
        if not include_archived:
            # status 可空：NULL 视作 ACTIVE，归档分支默认从导航隐藏。
            query = query.where((ConversationBranchModel.status.is_(None)) | (ConversationBranchModel.status != "ARCHIVED"))
        rows = (await self.session.scalars(query.order_by(ConversationBranchModel.id))).all()
        return [ConversationBranch(id=row.id, conversation_id=row.conversation_id, name=row.name, parent_branch_id=row.parent_branch_id, forked_from_message_id=row.forked_from_message_id, status=row.status or "ACTIVE", title=row.title) for row in rows]

    async def archive_branch(self, branch_id: str, conversation_id: str, owner_id: str) -> bool:
        from datetime import UTC, datetime
        if not await self.branch_belongs_to_conversation(branch_id, conversation_id, owner_id):
            return False
        row = await self.session.get(ConversationBranchModel, branch_id)
        row.status = "ARCHIVED"
        row.archived_at = datetime.now(UTC)
        await self.session.flush()
        return True

    async def delete_owned(self, conversation_id: str, owner_id: str) -> bool:
        """Delete a conversation and all child rows, in FK-safe order.

        No ORM cascade exists, so children are deleted manually:
        usage refs nullified (accounting rows are kept) → conversation_events →
        message_chunks/message_edits (via message id subquery) → messages →
        branches → conversation. ContextSnapshots are project-level shared and
        deliberately kept. Caller commits; returns False when not found/owned.
        """
        if not await self.belongs_to_owner(conversation_id, owner_id):
            return False
        from proseforge.infrastructure.database.models.usage import (
            ModelUsageRecordModel,
        )

        branch_ids = select(ConversationBranchModel.id).where(ConversationBranchModel.conversation_id == conversation_id)
        message_ids = select(MessageModel.id).where(MessageModel.branch_id.in_(branch_ids))
        # Usage records are user-level accounting history: keep the rows but
        # drop the dangling conversation/message references.
        await self.session.execute(
            update(ModelUsageRecordModel)
            .where(ModelUsageRecordModel.conversation_id == conversation_id)
            .values(conversation_id=None, message_id=None)
        )
        await self.session.execute(delete(ConversationEventModel).where(ConversationEventModel.conversation_id == conversation_id))
        await self.session.execute(delete(MessageChunkModel).where(MessageChunkModel.message_id.in_(message_ids)))
        await self.session.execute(delete(MessageEditModel).where(MessageEditModel.message_id.in_(message_ids)))
        await self.session.execute(delete(MessageModel).where(MessageModel.branch_id.in_(branch_ids)))
        await self.session.execute(delete(ConversationBranchModel).where(ConversationBranchModel.conversation_id == conversation_id))
        await self.session.execute(delete(ConversationModel).where(ConversationModel.id == conversation_id))
        await self.session.flush()
        return True

    async def set_message_status(self, message_id: str, status: str) -> None:
        row = await self.session.get(MessageModel, message_id)
        if row is None:
            raise ValueError("message does not exist")
        if row.status == "CANCELLED" and status in {"STREAMING", "COMPLETED", "PARTIAL"}:
            return
        row.status = status
        await self.session.flush()

    async def cancel_message_if_active(self, message_id: str, cancellable: set[str]) -> bool:
        """Conditional cancel: flip to CANCELLED only while still cancellable.

        A plain read-check-write races the worker (it can COMPLETE between the
        route's status check and the status write, resurrecting a finished
        message as CANCELLED with no way back). The WHERE clause makes the
        transition atomic; False means the race was lost and the route
        answers 409.
        """
        result = await self.session.execute(
            update(MessageModel)
            .where(MessageModel.id == message_id, MessageModel.status.in_(sorted(cancellable)))
            .values(status="CANCELLED")
        )
        return bool(result.rowcount)

    async def message_status(self, message_id: str) -> str | None:
        row = await self.session.get(MessageModel, message_id)
        return row.status if row else None

    async def set_message_content(self, message_id: str, content: str) -> None:
        """Overwrite a message's content (file-block rewrite on completion).

        Callers must recompute content_hash afterwards from the new content.
        """
        row = await self.session.get(MessageModel, message_id)
        if row is None:
            raise ValueError("message does not exist")
        row.content = content
        await self.session.flush()

    async def set_content_hash(self, message_id: str, content_hash: str) -> None:
        row = await self.session.get(MessageModel, message_id)
        if row is None:
            raise ValueError("message does not exist")
        row.content_hash = content_hash
        await self.session.flush()

    async def set_message_snapshots(self, message_id: str, model_snapshot: dict, reasoning_snapshot: dict) -> None:
        row = await self.session.get(MessageModel, message_id)
        if row is None:
            raise ValueError("message does not exist")
        row.model_snapshot_json = json.dumps(model_snapshot, ensure_ascii=False, sort_keys=True)
        row.reasoning_snapshot_json = json.dumps(reasoning_snapshot, ensure_ascii=False, sort_keys=True)
        await self.session.flush()

    async def project_id_for_message(self, message_id: str) -> str | None:
        return await self.session.scalar(
            select(ConversationModel.project_id)
            .join(ConversationBranchModel, ConversationBranchModel.conversation_id == ConversationModel.id)
            .join(MessageModel, MessageModel.branch_id == ConversationBranchModel.id)
            .where(MessageModel.id == message_id)
        )

    async def set_message_agent_run(self, message_id: str, agent_run_id: str | None) -> None:
        """Link/unlink the swarm agent run that owns this assistant message."""
        row = await self.session.get(MessageModel, message_id)
        if row is None:
            raise ValueError("message does not exist")
        row.agent_run_id = agent_run_id
        await self.session.flush()

    async def message_for_agent_run(self, agent_run_id: str) -> Message | None:
        row = await self.session.scalar(
            select(MessageModel).where(MessageModel.agent_run_id == agent_run_id)
        )
        return self._message(row) if row else None

    @staticmethod
    def _message(item: MessageModel) -> Message:
        model_snapshot = json.loads(item.model_snapshot_json) if item.model_snapshot_json else None
        reasoning_snapshot = json.loads(item.reasoning_snapshot_json) if item.reasoning_snapshot_json else None
        return Message(id=item.id, branch_id=item.branch_id, role=item.role, content=item.content, client_request_id=item.client_request_id, status=item.status, parent_message_id=item.parent_message_id, generation_attempt=item.generation_attempt or 1, model_snapshot=model_snapshot, reasoning_snapshot=reasoning_snapshot, content_hash=item.content_hash, agent_run_id=item.agent_run_id)

    @staticmethod
    def _chunk(item: MessageChunkModel) -> MessageChunk:
        return MessageChunk(id=item.id, message_id=item.message_id, chunk_index=item.chunk_index, event_type=item.event_type, content=item.content)
