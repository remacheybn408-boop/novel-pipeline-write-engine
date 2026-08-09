"""Project delete cascade completeness (sqlite in-memory via aiosqlite).

Builds one row in every project-owned table (with FK enforcement ON, so the
insert order itself proves the parent chain), deletes the project through the
repository, and asserts every child table is empty, usage accounting rows are
kept with nulled refs, and shared blob keys survive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.infrastructure.database import (
    models,  # noqa: F401  # register metadata
)
from proseforge.infrastructure.database.base import Base
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
from proseforge.infrastructure.database.models.project import ProjectModel
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
from proseforge.infrastructure.database.models.story_bible import StoryBibleEntryModel
from proseforge.infrastructure.database.models.usage import ModelUsageRecordModel
from proseforge.infrastructure.database.models.workflow_v2 import (
    WorkflowDefinitionModel,
    WorkflowNodeStateModel,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)
from tests.conftest import make_fk_engine

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)

# Every model whose rows must be gone after the project delete.
_PROJECT_CHILD_TABLES = (
    ConversationModel, ConversationBranchModel, MessageModel, MessageEditModel, MessageChunkModel,
    ConversationEventModel, ChapterModel, ChapterVersionModel, RevisionProposalModel,
    WorkflowRunModel, WorkflowDefinitionModel, WorkflowStepModel, WorkflowEventModel, ModelCallModel,
    WorkflowNodeStateModel, AgentRunModel, AgentTaskModel, AgentEventModel, AgentArtifactModel,
    AgentReviewModel, AgentPolicySnapshotModel, AgentEvaluationModel, AgentMemoryModel,
    AgentGraphRevisionModel, OutlineModel, OutlineVersionModel, StoryBibleEntryModel,
    ReviewReportModel, ExportManifestModel, QualityReportModel, ContextItemModel,
    ContextSnapshotModel, ArtifactModel, AttachmentModel, CharacterModel,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_full_graph(factory) -> None:
    """Insert one full project subtree for (u1, p1) plus a neighbor project
    (u1, p2) whose attachment shares p1's blob key."""
    async with factory() as session:
        session.add(ProjectModel(id="p1", owner_id="u1", slug="p1", title="P1", mode="work"))
        session.add(ProjectModel(id="p2", owner_id="u1", slug="p2", title="P2", mode="chat"))
        await session.flush()

        # Conversation chain (flush per layer: the ORM does not sort inserts
        # by table-level FKs, and this engine enforces them).
        session.add(ConversationModel(id="cv1", project_id="p1", title="t"))
        await session.flush()
        session.add(ConversationBranchModel(id="b1", conversation_id="cv1", name="main"))
        await session.flush()
        session.add(MessageModel(id="m1", branch_id="b1", role="user", content="hi", sequence_no=1))
        await session.flush()
        session.add(MessageEditModel(id="me1", message_id="m1", original_content="a", edited_content="b", created_branch_id="b1", created_at=NOW))
        session.add(MessageChunkModel(id="mc1", message_id="m1", chunk_index=0, event_type="delta", content="x"))
        session.add(ConversationEventModel(id="ce1", conversation_id="cv1", event_sequence=1, event_type="e", payload="{}"))
        # Message-stream event (polymorphic key = message id, no FK): the
        # cascade must sweep this key space too.
        session.add(ConversationEventModel(id="ce2", conversation_id="m1", event_sequence=2, event_type="e", payload="{}"))
        session.add(ModelUsageRecordModel(id="u1r", user_id="u1", project_id="p1", conversation_id="cv1", message_id="m1", provider="p", model_id="m", call_id="call-1", created_at=NOW))

        # Chapter chain.
        session.add(ChapterModel(id="ch1", project_id="p1", chapter_no=1, title="c"))
        await session.flush()
        session.add(ChapterVersionModel(id="cvv1", chapter_id="ch1", version_no=1, content="text", content_hash="h", word_count=1))
        session.add(RevisionProposalModel(id="rp1", chapter_id="ch1", base_version_id="cvv1", before_hash="a", after_text="b", after_hash="c", rationale="r", created_at=NOW))

        # Legacy workflow chain.
        session.add(WorkflowRunModel(id="wr1", project_id="p1", workflow_type="NOVEL", status="QUEUED"))
        session.add(WorkflowDefinitionModel(id="wd1", project_id="p1", name="n", revision=1, definition_json="{}", created_at=NOW, updated_at=NOW))
        await session.flush()
        session.add(WorkflowStepModel(id="ws1", workflow_run_id="wr1", idempotency_key="k", status="DONE"))
        session.add(WorkflowEventModel(id="we1", workflow_run_id="wr1", sequence_no=1, event_type="e", payload="{}"))
        session.add(ModelCallModel(id="mcl1", workflow_run_id="wr1", provider="p", model_id="m"))
        session.add(WorkflowNodeStateModel(id="wn1", run_id="wr1", node_key="n", updated_at=NOW))

        # Agent chain.
        session.add(AgentRunModel(id="ar1", user_id="u1", project_id="p1", goal_hash="g", graph_revision=1, created_at=NOW, updated_at=NOW))
        await session.flush()
        session.add(AgentTaskModel(id="at1", run_id="ar1", task_key="t", role="r"))
        session.add(AgentEventModel(id="ae1", run_id="ar1", sequence=1, event_type="e"))
        session.add(AgentArtifactModel(id="aa1", run_id="ar1", artifact_type="t", sha256="s"))
        session.add(AgentReviewModel(id="arv1", run_id="ar1", artifact_id="aa1", reviewer_role="r", status="OK"))
        session.add(AgentPolicySnapshotModel(id="aps1", run_id="ar1", policy_version="v", policy_hash="h"))
        session.add(AgentEvaluationModel(id="aev1", run_id="ar1", fixture_hash="f", score=1))
        session.add(AgentMemoryModel(id="am1", project_id="p1", run_id="ar1", memory_key="k", value="v", source_artifact_id="aa1"))
        # Project-wide memory (PROJECT_WIDE_RUN sentinel, no backing run row).
        session.add(AgentMemoryModel(id="am2", project_id="p1", run_id="", memory_key="k2", value="v", source_artifact_id="aa1"))
        session.add(AgentGraphRevisionModel(id="ag1", project_id="p1", revision=1, graph_hash="g"))

        # Outline chain.
        session.add(OutlineModel(id="o1", project_id="p1", title="o"))
        await session.flush()
        session.add(OutlineVersionModel(id="ov1", outline_id="o1", version_no=1, payload="{}"))

        # Flat project-owned tables.
        session.add(StoryBibleEntryModel(id="sb1", project_id="p1", kind="k", key="k", value_json="{}", created_at=NOW, updated_at=NOW))
        session.add(CharacterModel(id="char1", project_id="p1", name="李雷", aliases_json="[]", summary="", role="", status="active", source="user", confidence=1.0, created_at=NOW, updated_at=NOW))
        session.add(ReviewReportModel(id="rr1", project_id="p1", scope="s", subject_type="chapter", subject_id="ch1", findings_json="[]", scores_json="{}", model_snapshot_json="{}", created_at=NOW))
        session.add(ExportManifestModel(id="em1", project_id="p1", user_id="u1", format="md", template="t", locale="zh", version_ids_json="[]", content_hashes_json="{}", file_sha256="f", byte_size=1, created_at=NOW))
        session.add(QualityReportModel(id="qr1", project_id="p1", subject_type="chapter", subject_id="ch1", report="{}"))
        session.add(ContextItemModel(id="ci1", project_id="p1", source_type="s", source_id="s", content="c"))
        session.add(ContextSnapshotModel(id="cs1", project_id="p1", snapshot_hash="h", payload="{}"))
        session.add(ArtifactModel(id="art1", project_id="p1", artifact_type="t", storage_key="sha256/aa/art", sha256="s"))
        session.add(AttachmentModel(id="att1", project_id="p1", filename="a.txt", sha256="s", storage_key="sha256/bb/shared"))
        # p2's attachment references the same blob key as p1's attachment.
        session.add(AttachmentModel(id="att2", project_id="p2", filename="b.txt", sha256="s", storage_key="sha256/bb/shared"))
        await session.commit()


@pytest.mark.asyncio
async def test_delete_cascades_every_project_table(session_factory):
    await _seed_full_graph(session_factory)
    async with session_factory() as session:
        repo = SqlAlchemyProjectRepository(session)
        blob_keys = await repo.delete("u1", "p1")
        await session.commit()

    assert blob_keys == ["sha256/aa/art"]  # shared key still referenced by p2
    async with session_factory() as session:
        for model in _PROJECT_CHILD_TABLES:
            # Only p2's shared-key attachment may survive; p1 owned everything else.
            total = await session.scalar(select(func.count(model.id)))
            expected = 1 if model is AttachmentModel else 0
            assert total == expected, f"{model.__tablename__} still has rows after p1 delete"
        # Usage accounting row kept, refs nullified.
        usage = await session.get(ModelUsageRecordModel, "u1r")
        assert usage is not None
        assert (usage.project_id, usage.conversation_id, usage.message_id) == (None, None, None)
        # Neighbor project and its attachment untouched.
        assert await session.get(ProjectModel, "p2") is not None
        assert await session.get(AttachmentModel, "att2") is not None


@pytest.mark.asyncio
async def test_delete_missing_or_foreign_project_returns_none(session_factory):
    await _seed_full_graph(session_factory)
    async with session_factory() as session:
        repo = SqlAlchemyProjectRepository(session)
        assert await repo.delete("u1", "nope") is None
        assert await repo.delete("other-user", "p1") is None
        await session.rollback()
    async with session_factory() as session:
        assert await session.get(ProjectModel, "p1") is not None
