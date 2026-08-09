from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from proseforge.domain.project.entity import Project
from proseforge.infrastructure.database.models.project import ProjectModel


class SqlAlchemyProjectRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, project: Project) -> Project:
        self.session.add(ProjectModel(**project.__dict__))
        # Flush immediately: callers insert child rows (chapters, conversations,
        # ...) in the same transaction, and the ORM does not order inserts by
        # table-level FK dependencies — the parent must hit the database first.
        await self.session.flush()
        return project

    async def get_by_slug(self, owner_id: str, slug: str) -> Project | None:
        result = await self.session.execute(
            select(ProjectModel).where(ProjectModel.owner_id == owner_id, ProjectModel.slug == slug)
        )
        row = result.scalar_one_or_none()
        return None if row is None else self._entity(row)

    async def get_by_id(self, owner_id: str, project_id: str) -> Project | None:
        row = await self.session.scalar(
            select(ProjectModel).where(ProjectModel.owner_id == owner_id, ProjectModel.id == project_id)
        )
        return None if row is None else self._entity(row)

    async def list_for_owner(self, owner_id: str, mode: str | None = None, archived: bool = False) -> list[Project]:
        # archived=False (default) hides ARCHIVED projects; True lists only archived.
        # The default filter is status != "ARCHIVED" (not == "ACTIVE") to stay
        # tolerant of any other status values in existing data.
        query = select(ProjectModel).where(ProjectModel.owner_id == owner_id)
        query = query.where(ProjectModel.status == "ARCHIVED") if archived else query.where(ProjectModel.status != "ARCHIVED")
        if mode is not None:
            query = query.where(ProjectModel.mode == mode)
        rows = await self.session.scalars(query.order_by(ProjectModel.title, ProjectModel.id))
        return [self._entity(row) for row in rows]

    async def update(self, owner_id: str, project_id: str, *, title: str | None = None, genre: str | None = None, style: str | None = None) -> Project | None:
        row = await self.session.scalar(
            select(ProjectModel).where(ProjectModel.owner_id == owner_id, ProjectModel.id == project_id)
        )
        if row is None:
            return None
        for field, value in (("title", title), ("genre", genre), ("style", style)):
            if value is not None:
                setattr(row, field, value)
        await self.session.flush()
        return self._entity(row)

    async def set_status(self, owner_id: str, project_id: str, status: str) -> Project | None:
        row = await self.session.scalar(
            select(ProjectModel).where(ProjectModel.owner_id == owner_id, ProjectModel.id == project_id)
        )
        if row is None:
            return None
        row.status = status
        await self.session.flush()
        return self._entity(row)

    async def lock_writing_model(self, project_id: str, *, provider: str, model_id: str, source: str) -> bool:
        """First-come-first-served writing-model lock; returns True when THIS
        call won the lock. Atomic guard keeps concurrent triggers idempotent."""
        from datetime import UTC, datetime

        from sqlalchemy import update

        result = await self.session.execute(
            update(ProjectModel)
            .where(ProjectModel.id == project_id, ProjectModel.model_locked_at.is_(None))
            .values(
                writing_model_provider=provider,
                writing_model_id=model_id,
                model_locked_at=datetime.now(UTC),
                model_lock_source=source,
            )
        )
        await self.session.flush()
        return bool(result.rowcount)

    async def delete(self, owner_id: str, project_id: str) -> list[str] | None:
        """Delete a project and every project-owned row, in FK-safe order.

        No ORM cascade exists, so children are deleted manually following the
        delete_owned precedent: usage/accounting rows are kept with their
        project refs nullified, everything else goes children-first. Returns
        the blob storage keys that lost their last reference (caller deletes
        the files after commit), or None when the project is missing/foreign.
        """
        from proseforge.infrastructure.database.models.agents import (
            AgentArtifactModel,
            AgentEvaluationModel,
            AgentEventModel,
            AgentGraphRevisionModel,
            AgentMemoryModel,
            AgentPolicySnapshotModel,
            AgentReviewModel,
            AgentRunModel,
            AgentTaskModel,
        )
        from proseforge.infrastructure.database.models.chapter import (
            ChapterModel,
            ChapterVersionModel,
        )
        from proseforge.infrastructure.database.models.character import CharacterModel
        from proseforge.infrastructure.database.models.conversation import (
            ConversationBranchModel,
            ConversationEventModel,
            ConversationModel,
            MessageChunkModel,
            MessageEditModel,
            MessageModel,
        )
        from proseforge.infrastructure.database.models.export import ExportManifestModel
        from proseforge.infrastructure.database.models.remaining import (
            ArtifactModel,
            AttachmentModel,
            ContextItemModel,
            ContextSnapshotModel,
            ModelCallModel,
            OutlineModel,
            OutlineVersionModel,
            QualityReportModel,
            WorkflowEventModel,
            WorkflowRunModel,
            WorkflowStepModel,
        )
        from proseforge.infrastructure.database.models.revision import (
            ReviewReportModel,
            RevisionProposalModel,
        )
        from proseforge.infrastructure.database.models.story_bible import (
            StoryBibleEntryModel,
        )
        from proseforge.infrastructure.database.models.usage import (
            ModelUsageRecordModel,
        )
        from proseforge.infrastructure.database.models.workflow_v2 import (
            WorkflowDefinitionModel,
            WorkflowNodeStateModel,
        )

        if await self.get_by_id(owner_id, project_id) is None:
            return None

        # Blob keys referenced by this project's attachments/artifacts, taken
        # before any row is deleted; deduped and re-checked for remaining
        # references at the end.
        blob_keys = set(
            await self.session.scalars(select(AttachmentModel.storage_key).where(AttachmentModel.project_id == project_id))
        ) | set(
            await self.session.scalars(select(ArtifactModel.storage_key).where(ArtifactModel.project_id == project_id))
        )

        # Usage records are user-level accounting history: keep the rows but
        # drop every dangling project-scoped reference.
        await self.session.execute(
            update(ModelUsageRecordModel)
            .where(ModelUsageRecordModel.project_id == project_id)
            .values(project_id=None, conversation_id=None, message_id=None, workflow_run_id=None)
        )

        # Conversation chain: chunks/edits → events → messages → branches → conversations.
        # conversation_events rows are keyed by polymorphic stream id: both
        # conversation ids and message ids occur (DatabaseEventStream), so
        # both key spaces must be swept.
        conversation_ids = select(ConversationModel.id).where(ConversationModel.project_id == project_id)
        branch_ids = select(ConversationBranchModel.id).where(ConversationBranchModel.conversation_id.in_(conversation_ids))
        message_ids = select(MessageModel.id).where(MessageModel.branch_id.in_(branch_ids))
        await self.session.execute(delete(MessageChunkModel).where(MessageChunkModel.message_id.in_(message_ids)))
        await self.session.execute(delete(MessageEditModel).where(MessageEditModel.message_id.in_(message_ids)))
        await self.session.execute(delete(ConversationEventModel).where(ConversationEventModel.conversation_id.in_(conversation_ids)))
        await self.session.execute(delete(ConversationEventModel).where(ConversationEventModel.conversation_id.in_(message_ids)))
        await self.session.execute(delete(MessageModel).where(MessageModel.branch_id.in_(branch_ids)))
        await self.session.execute(delete(ConversationBranchModel).where(ConversationBranchModel.conversation_id.in_(conversation_ids)))
        await self.session.execute(delete(ConversationModel).where(ConversationModel.project_id == project_id))

        # Chapter chain: proposals → versions → chapters.
        chapter_ids = select(ChapterModel.id).where(ChapterModel.project_id == project_id)
        await self.session.execute(delete(RevisionProposalModel).where(RevisionProposalModel.chapter_id.in_(chapter_ids)))
        await self.session.execute(delete(ChapterVersionModel).where(ChapterVersionModel.chapter_id.in_(chapter_ids)))
        await self.session.execute(delete(ChapterModel).where(ChapterModel.project_id == project_id))

        # Legacy workflow chain: node states/steps/events/calls → runs → definitions.
        workflow_run_ids = select(WorkflowRunModel.id).where(WorkflowRunModel.project_id == project_id)
        await self.session.execute(delete(WorkflowNodeStateModel).where(WorkflowNodeStateModel.run_id.in_(workflow_run_ids)))
        await self.session.execute(delete(WorkflowStepModel).where(WorkflowStepModel.workflow_run_id.in_(workflow_run_ids)))
        await self.session.execute(delete(WorkflowEventModel).where(WorkflowEventModel.workflow_run_id.in_(workflow_run_ids)))
        await self.session.execute(delete(ModelCallModel).where(ModelCallModel.workflow_run_id.in_(workflow_run_ids)))
        await self.session.execute(delete(WorkflowRunModel).where(WorkflowRunModel.project_id == project_id))
        await self.session.execute(delete(WorkflowDefinitionModel).where(WorkflowDefinitionModel.project_id == project_id))

        # Agent chain: run children → memories → runs → graph revisions.
        agent_run_ids = select(AgentRunModel.id).where(AgentRunModel.project_id == project_id)
        await self.session.execute(delete(AgentTaskModel).where(AgentTaskModel.run_id.in_(agent_run_ids)))
        await self.session.execute(delete(AgentEventModel).where(AgentEventModel.run_id.in_(agent_run_ids)))
        await self.session.execute(delete(AgentArtifactModel).where(AgentArtifactModel.run_id.in_(agent_run_ids)))
        await self.session.execute(delete(AgentReviewModel).where(AgentReviewModel.run_id.in_(agent_run_ids)))
        await self.session.execute(delete(AgentPolicySnapshotModel).where(AgentPolicySnapshotModel.run_id.in_(agent_run_ids)))
        await self.session.execute(delete(AgentEvaluationModel).where(AgentEvaluationModel.run_id.in_(agent_run_ids)))
        # Project-scoped memories include PROJECT_WIDE_RUN ("") sentinel rows
        # whose run_id matches nothing, so sweep by project_id, not run_id.
        await self.session.execute(delete(AgentMemoryModel).where(AgentMemoryModel.project_id == project_id))
        await self.session.execute(delete(AgentRunModel).where(AgentRunModel.project_id == project_id))
        await self.session.execute(delete(AgentGraphRevisionModel).where(AgentGraphRevisionModel.project_id == project_id))

        # Outline chain: versions → outlines.
        outline_ids = select(OutlineModel.id).where(OutlineModel.project_id == project_id)
        await self.session.execute(delete(OutlineVersionModel).where(OutlineVersionModel.outline_id.in_(outline_ids)))
        await self.session.execute(delete(OutlineModel).where(OutlineModel.project_id == project_id))

        # Flat project-owned tables.
        await self.session.execute(delete(StoryBibleEntryModel).where(StoryBibleEntryModel.project_id == project_id))
        await self.session.execute(delete(CharacterModel).where(CharacterModel.project_id == project_id))
        await self.session.execute(delete(ReviewReportModel).where(ReviewReportModel.project_id == project_id))
        await self.session.execute(delete(ExportManifestModel).where(ExportManifestModel.project_id == project_id))
        await self.session.execute(delete(QualityReportModel).where(QualityReportModel.project_id == project_id))
        await self.session.execute(delete(ContextItemModel).where(ContextItemModel.project_id == project_id))
        await self.session.execute(delete(ContextSnapshotModel).where(ContextSnapshotModel.project_id == project_id))
        await self.session.execute(delete(ArtifactModel).where(ArtifactModel.project_id == project_id))
        await self.session.execute(delete(AttachmentModel).where(AttachmentModel.project_id == project_id))

        await self.session.execute(delete(ProjectModel).where(ProjectModel.id == project_id))
        await self.session.flush()

        orphaned: list[str] = []
        for storage_key in sorted(blob_keys):
            attachment_count = await self.session.scalar(select(func.count(AttachmentModel.id)).where(AttachmentModel.storage_key == storage_key))
            artifact_count = await self.session.scalar(select(func.count(ArtifactModel.id)).where(ArtifactModel.storage_key == storage_key))
            if not (attachment_count or artifact_count):
                orphaned.append(storage_key)
        return orphaned

    @staticmethod
    def _entity(row: ProjectModel) -> Project:
        return Project(
            id=row.id,
            owner_id=row.owner_id,
            slug=row.slug,
            title=row.title,
            genre=row.genre,
            style=row.style,
            language=row.language,
            status=row.status,
            mode=row.mode,
            writing_model_provider=row.writing_model_provider,
            writing_model_id=row.writing_model_id,
            model_locked_at=row.model_locked_at,
            model_lock_source=row.model_lock_source,
        )
