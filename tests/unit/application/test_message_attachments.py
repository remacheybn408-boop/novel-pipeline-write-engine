"""Attachment injection helpers: parse blocks, token trim, history augment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proseforge.application.files.message_attachments import (
    TRUNCATED_NOTE,
    UNPARSEABLE_NOTE,
    InjectedMessage,
    _trim_to_token_budget,
    inject_history_attachments,
    parse_attachment_blocks,
    prepend_blocks,
)
from proseforge.context_engine.tokenizer import ConservativeTokenizer
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database.base import Base
from proseforge.infrastructure.database.models.remaining import AttachmentModel


def _attachment(attachment_id: str, filename: str, storage_key: str) -> SimpleNamespace:
    return SimpleNamespace(id=attachment_id, filename=filename, storage_key=storage_key)


@pytest.mark.asyncio
async def test_parse_attachment_blocks_renders_prefix_block(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    storage_key = await store.put(data="文件正文内容".encode(), media_type="text/plain")

    blocks = await parse_attachment_blocks(str(tmp_path), [_attachment("a1", "notes.txt", storage_key)])

    assert len(blocks) == 1
    assert blocks[0].text == "[附件: notes.txt]\n文件正文内容"


@pytest.mark.asyncio
async def test_parse_attachment_blocks_unparseable_degrades_to_note(tmp_path):
    # Missing blob: honest note block instead of an exception.
    blocks = await parse_attachment_blocks(str(tmp_path), [_attachment("a1", "gone.txt", "missing-key")])

    assert blocks[0].text == f"[附件: gone.txt]\n{UNPARSEABLE_NOTE}"


def test_trim_to_token_budget_truncates_by_tokens():
    tokenizer = ConservativeTokenizer()
    text = "字" * 10_000  # CJK: 1 token per char -> way over a small budget

    trimmed = _trim_to_token_budget(text, 1000, tokenizer)

    assert trimmed.endswith(TRUNCATED_NOTE)
    assert tokenizer.count(trimmed) <= 1000 + tokenizer.count(TRUNCATED_NOTE) + 1


def test_trim_to_token_budget_passes_short_text():
    assert _trim_to_token_budget("short", 1000, ConservativeTokenizer()) == "short"


def test_prepend_blocks_orders_attachments_before_content():
    blocks = [SimpleNamespace(text="[附件: a.txt]\nA"), SimpleNamespace(text="[附件: b.txt]\nB")]
    assert prepend_blocks("用户问题", blocks) == "[附件: a.txt]\nA\n\n[附件: b.txt]\nB\n\n用户问题"
    assert prepend_blocks("用户问题", []) == "用户问题"


@pytest.mark.asyncio
async def test_inject_history_attachments_augments_only_linked_messages(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'att.db').as_posix()}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        store = LocalBlobStore(str(tmp_path / "blobs"))
        storage_key = await store.put(data="注入的正文".encode(), media_type="text/plain")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add(AttachmentModel(id="att-1", project_id="p1", filename="notes.txt", sha256="x" * 64, storage_key=storage_key, message_id="m1"))
            await session.commit()
        history = [
            SimpleNamespace(id="m0", role="user", content="第一条"),
            SimpleNamespace(id="m1", role="user", content="请看附件"),
            SimpleNamespace(id="m2", role="assistant", content="好的"),
        ]

        async with session_factory() as session:
            augmented = await inject_history_attachments(session, str(tmp_path / "blobs"), history)

        assert augmented[0] is history[0]  # untouched messages pass through
        assert augmented[2] is history[2]
        injected = augmented[1]
        assert isinstance(injected, InjectedMessage)
        assert injected.content == "[附件: notes.txt]\n注入的正文\n\n请看附件"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inject_history_attachments_no_attachments_returns_originals(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'plain.db').as_posix()}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        history = [SimpleNamespace(id="m1", role="user", content="你好")]
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            augmented = await inject_history_attachments(session, str(tmp_path / "blobs"), history)
        assert augmented[0] is history[0]
    finally:
        await engine.dispose()
