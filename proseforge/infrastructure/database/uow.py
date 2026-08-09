from __future__ import annotations

from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from proseforge.infrastructure.database.repositories.attachment import (
    SqlAlchemyAttachmentRepository,
)
from proseforge.infrastructure.database.repositories.builtin_skill_state import (
    SqlAlchemyBuiltinSkillStateRepository,
)
from proseforge.infrastructure.database.repositories.chapter import (
    SqlAlchemyChapterRepository,
)
from proseforge.infrastructure.database.repositories.character import (
    SqlAlchemyCharacterRepository,
)
from proseforge.infrastructure.database.repositories.context import (
    SqlAlchemyContextRepository,
)
from proseforge.infrastructure.database.repositories.conversation import (
    SqlAlchemyConversationRepository,
)
from proseforge.infrastructure.database.repositories.credential import (
    SqlAlchemyCredentialRepository,
)
from proseforge.infrastructure.database.repositories.export import (
    SqlAlchemyExportManifestRepository,
)
from proseforge.infrastructure.database.repositories.knowledge import (
    SqlAlchemyKnowledgeRepository,
)
from proseforge.infrastructure.database.repositories.mcp_server import (
    SqlAlchemyMcpServerRepository,
)
from proseforge.infrastructure.database.repositories.model_catalog import (
    SqlAlchemyModelCatalogRepository,
)
from proseforge.infrastructure.database.repositories.model_profile import (
    SqlAlchemyModelProfileRepository,
)
from proseforge.infrastructure.database.repositories.outline import (
    SqlAlchemyOutlineRepository,
)
from proseforge.infrastructure.database.repositories.project import (
    SqlAlchemyProjectRepository,
)
from proseforge.infrastructure.database.repositories.retrieval import (
    SqlAlchemyRetrievalRepository,
)
from proseforge.infrastructure.database.repositories.revision import (
    SqlAlchemyRevisionRepository,
)
from proseforge.infrastructure.database.repositories.skill import (
    SqlAlchemySkillRepository,
)
from proseforge.infrastructure.database.repositories.tool_call import (
    SqlAlchemyToolCallRepository,
)
from proseforge.infrastructure.database.repositories.usage import (
    SqlAlchemyUsageRepository,
)
from proseforge.infrastructure.database.repositories.user import (
    SqlAlchemyUserRepository,
)
from proseforge.infrastructure.database.repositories.user_preference import (
    SqlAlchemyUserPreferenceRepository,
)
from proseforge.infrastructure.database.repositories.workflow import (
    SqlAlchemyWorkflowRepository,
)
from proseforge.infrastructure.database.session import close_session, rollback_session


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory
        self.session: AsyncSession | None = None
        self._committed = False
        self.projects = None
        self.chapters = None
        self.conversations = None
        self.messages = None
        self.workflows = None
        self.attachments = None
        self.model_catalog = None
        self.users = None
        self.credentials = None
        self.outlines = None
        self.context = None
        self.model_profiles = None
        self.usage = None
        self.revisions = None
        self.exports = None
        self.skills = None
        self.mcp_servers = None
        self.builtin_skill_states = None
        self.tool_calls = None
        self.retrieval = None
        self.user_preferences = None
        self.characters = None
        self.knowledge = None

    async def __aenter__(self) -> Self:
        self.session = self.session_factory()
        self._committed = False
        self.projects = SqlAlchemyProjectRepository(self.session)
        self.chapters = SqlAlchemyChapterRepository(self.session)
        self.conversations = SqlAlchemyConversationRepository(self.session)
        self.model_catalog = SqlAlchemyModelCatalogRepository(self.session)
        self.workflows = SqlAlchemyWorkflowRepository(self.session)
        self.attachments = SqlAlchemyAttachmentRepository(self.session)
        self.users = SqlAlchemyUserRepository(self.session)
        self.credentials = SqlAlchemyCredentialRepository(self.session)
        self.outlines = SqlAlchemyOutlineRepository(self.session)
        self.context = SqlAlchemyContextRepository(self.session)
        self.model_profiles = SqlAlchemyModelProfileRepository(self.session)
        self.usage = SqlAlchemyUsageRepository(self.session)
        self.revisions = SqlAlchemyRevisionRepository(self.session)
        self.exports = SqlAlchemyExportManifestRepository(self.session)
        self.skills = SqlAlchemySkillRepository(self.session)
        self.mcp_servers = SqlAlchemyMcpServerRepository(self.session)
        self.builtin_skill_states = SqlAlchemyBuiltinSkillStateRepository(self.session)
        self.tool_calls = SqlAlchemyToolCallRepository(self.session)
        self.retrieval = SqlAlchemyRetrievalRepository(self.session)
        self.user_preferences = SqlAlchemyUserPreferenceRepository(self.session)
        self.characters = SqlAlchemyCharacterRepository(self.session)
        self.knowledge = SqlAlchemyKnowledgeRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None or not self._committed:
                # Cancellation-resilient teardown: a client abort can deliver
                # CancelledError inside rollback/close; retrying completes the
                # connection checkin instead of leaking it to the pool GC.
                await rollback_session(self.session)
        finally:
            await close_session(self.session)
            self.session = None

    async def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        if self.session is not None:
            await self.session.rollback()
        self._committed = False
