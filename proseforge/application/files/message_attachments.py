"""User-message attachments: link uploads to messages and inject parsed text.

Upload flow: the client uploads files first (POST /projects/{id}/files,
``attachments.message_id`` still NULL), then sends their ids on the message.
The send path links the rows to the persisted user message; generation
injects the parsed text as ``[附件: 文件名]`` prefix blocks on that message's
content, so both the chat context compiler and swarm run goals see the file
body without changing the persisted message content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select, update

from proseforge.context_engine.tokenizer import ConservativeTokenizer
from proseforge.infrastructure.blob.local import LocalBlobStore
from proseforge.infrastructure.database.models.remaining import AttachmentModel
from proseforge.infrastructure.webtools.documents import parse_document_bytes

# Per-attachment injection cap in estimated tokens (ConservativeTokenizer),
# aligned with the module-constant budget style (cf. DEFAULT_SWARM_BUDGET).
ATTACHMENT_INJECT_MAX_TOKENS = 30_000
# Character ceiling before token trimming: bounds parser output in memory.
ATTACHMENT_PARSE_MAX_CHARS = 400_000
TRUNCATED_NOTE = "[truncated: attachment exceeded the injection token budget]"
UNPARSEABLE_NOTE = "(could not extract text from this attachment)"


@dataclass(frozen=True)
class AttachmentBlock:
    attachment_id: str
    filename: str
    text: str  # rendered "[附件: 文件名]\n<body>" block


@dataclass(frozen=True)
class InjectedMessage:
    """History shim for a message whose content gained attachment blocks.

    CompileChatContext and the snapshot builder only read id/role/content,
    so the shim replaces the ORM object for augmented messages only.
    """

    id: str
    role: str
    content: str


async def link_attachments_to_message(session, attachment_ids: Iterable[str], message_id: str) -> None:
    """Point uploaded attachment rows at the persisted user message."""
    ids = list(attachment_ids)
    if not ids:
        return
    await session.execute(update(AttachmentModel).where(AttachmentModel.id.in_(ids)).values(message_id=message_id))


async def attachments_for_messages(session, message_ids: Iterable[str]) -> dict[str, list[AttachmentModel]]:
    """Group attachment rows by linked message id (deterministic order)."""
    ids = list(message_ids)
    if not ids:
        return {}
    rows = (
        await session.scalars(select(AttachmentModel).where(AttachmentModel.message_id.in_(ids)).order_by(AttachmentModel.id))
    ).all()
    grouped: dict[str, list[AttachmentModel]] = {}
    for row in rows:
        if row.message_id is not None:
            grouped.setdefault(row.message_id, []).append(row)
    return grouped


def _trim_to_token_budget(text: str, max_tokens: int, tokenizer: ConservativeTokenizer) -> str:
    tokens = tokenizer.count(text)
    if tokens <= max_tokens:
        return text
    # ConservativeTokenizer is ~linear in characters (CJK=1, else 0.25), so
    # scaling the cut by the token ratio lands under budget in one pass.
    keep = max(1, int(len(text) * max_tokens / tokens))
    return f"{text[:keep]}\n{TRUNCATED_NOTE}"


async def parse_attachment_blocks(
    blob_root: str,
    attachments: Iterable[AttachmentModel],
    *,
    tokenizer: ConservativeTokenizer | None = None,
    max_tokens: int = ATTACHMENT_INJECT_MAX_TOKENS,
) -> list[AttachmentBlock]:
    """Parse each attachment row into a ``[附件: 文件名]`` prefix block.

    An unreadable or unparseable file degrades to an honest note block
    instead of failing the whole generation.
    """
    store = LocalBlobStore(blob_root)
    counter = tokenizer or ConservativeTokenizer()
    blocks: list[AttachmentBlock] = []
    for attachment in attachments:
        try:
            data = await store.get(attachment.storage_key)
            text = await asyncio.to_thread(parse_document_bytes, data, attachment.filename)
            text = _trim_to_token_budget(text[:ATTACHMENT_PARSE_MAX_CHARS], max_tokens, counter)
        except Exception:
            text = UNPARSEABLE_NOTE
        blocks.append(AttachmentBlock(attachment_id=attachment.id, filename=attachment.filename, text=f"[附件: {attachment.filename}]\n{text}"))
    return blocks


def prepend_blocks(content: str, blocks: Iterable[AttachmentBlock]) -> str:
    """Attachment blocks go BEFORE the user's own text (prefix injection)."""
    rendered = [block.text for block in blocks]
    if not rendered:
        return content
    return "\n\n".join([*rendered, content])


async def inject_history_attachments(session, blob_root: str, history, *, tokenizer: ConservativeTokenizer | None = None) -> list:
    """Return history with user messages' attachment text prefixed to content.

    Messages without attachments pass through as the original objects, so
    the common no-attachment case costs one cheap query and nothing else.
    """
    grouped = await attachments_for_messages(session, [message.id for message in history if message.role == "user"])
    if not grouped:
        return list(history)
    augmented: list = []
    for message in history:
        rows = grouped.get(message.id)
        if not rows:
            augmented.append(message)
            continue
        blocks = await parse_attachment_blocks(blob_root, rows, tokenizer=tokenizer)
        augmented.append(InjectedMessage(id=message.id, role=message.role, content=prepend_blocks(message.content, blocks)))
    return augmented
