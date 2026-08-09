"""run_code input-file mode check (sqlite in-memory via aiosqlite).

Attachments whose project mode differs from the current conversation's
project mode must be refused with a per-file note, never copied into the
sandbox input set.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from proseforge.application.tools.builtin import _load_input_files
from proseforge.application.tools.types import ToolContext
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database import (
    models,  # noqa: F401  # register metadata
)
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.conversation import (
    ConversationBranchModel,
    ConversationModel,
    MessageModel,
)
from proseforge.infrastructure.database.models.project import ProjectModel
from proseforge.infrastructure.database.models.remaining import AttachmentModel
from tests.conftest import make_fk_engine


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = make_fk_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    blob_store = LocalBlobStore(tmp_path / "blobs")
    async with factory() as session:
        session.add(ProjectModel(id="pw", owner_id="u1", slug="pw", title="W", mode="work"))
        session.add(ProjectModel(id="pc", owner_id="u1", slug="pc", title="C", mode="chat"))
        await session.flush()
        # The current session lives in the chat project (flush per layer:
        # the ORM does not sort inserts by table-level FKs).
        session.add(ConversationModel(id="cv1", project_id="pc", title="t"))
        await session.flush()
        session.add(ConversationBranchModel(id="b1", conversation_id="cv1", name="main"))
        await session.flush()
        session.add(MessageModel(id="m1", branch_id="b1", role="user", content="hi", sequence_no=1))
        await session.flush()
        for attachment_id, project_id, filename, data in (
            ("att-chat", "pc", "note.txt", b"chat bytes"),
            ("att-work", "pw", "chapter.txt", b"work bytes"),
        ):
            storage_key = await blob_store.put(data=data, media_type="text/plain")
            session.add(AttachmentModel(id=attachment_id, project_id=project_id, filename=filename, sha256="s", storage_key=storage_key))
        await session.commit()
    ctx = ToolContext(settings=SimpleNamespace(blob_root=tmp_path / "blobs"), session_factory=factory, message_id="m1", user_id="u1")
    yield ctx
    await engine.dispose()


@pytest.mark.asyncio
async def test_cross_mode_attachment_is_refused(env):
    files, notes = await _load_input_files(["att-chat", "att-work"], env)
    assert [name for name, _ in files] == ["note.txt"]
    assert len(notes) == 1
    assert "chapter.txt" in notes[0] and "模式不一致" in notes[0]


@pytest.mark.asyncio
async def test_same_mode_attachment_loads(env):
    files, notes = await _load_input_files(["att-chat"], env)
    assert notes == []
    assert files == [("note.txt", b"chat bytes")]


@pytest.mark.asyncio
async def test_missing_message_context_skips_mode_check(env):
    # Without a resolvable session message the mode check cannot run; the
    # ownership check still applies (legacy behavior preserved).
    ctx = ToolContext(settings=env.settings, session_factory=env.session_factory, message_id="", user_id="u1")
    files, notes = await _load_input_files(["att-work"], ctx)
    assert notes == []
    assert [name for name, _ in files] == ["chapter.txt"]
