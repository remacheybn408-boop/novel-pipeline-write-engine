"""Extract ```file:<name> blocks from completed chat messages into attachments.

Protocol (injected via CHAT_PERSONA): when the user asks for a downloadable
file, the model emits one fenced block per file whose info string is
``file:<filename-with-extension>``. On message completion the worker swaps
each block for a download link plus a plain fenced preview (link on top,
preview below), stores the body as a blob, and registers an attachment row.

Failure philosophy: oversized bodies, missing project, blob/DB errors — all
leave the original text untouched. Extraction must never fail a generation.
"""

from __future__ import annotations

import hashlib
import re

MAX_FILE_BYTES = 8 * 1024 * 1024  # 8MB: far beyond any model's single-reply output, still memory-safe
MAX_FILENAME_CHARS = 120

_FILE_BLOCK = re.compile(r"```file:(?P<name>[^\n]*)\n(?P<body>.*?)```", re.DOTALL)

# Extension -> fence language for the degraded plain-code-block preview.
_LANG_BY_EXT = {
    "md": "markdown",
    "markdown": "markdown",
    "txt": "text",
    "csv": "csv",
    "tsv": "csv",
    "html": "html",
    "htm": "html",
    "json": "json",
    "py": "python",
    "js": "javascript",
    "yaml": "yaml",
    "yml": "yaml",
}


def sanitize_filename(raw: str, index: int) -> str:
    """Strip path separators/control chars, cap length; empty names get file-N.md."""
    name = re.sub(r"[\\/\x00-\x1f\x7f]", "", raw).strip().strip(".")
    name = name[:MAX_FILENAME_CHARS].strip()
    return name or f"file-{index}.md"


def _preview_lang(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _LANG_BY_EXT.get(extension, "")


async def extract_and_rewrite(uow, *, message_id: str, content: str, blob_store=None, project_id: str | None = None) -> str:
    """Persist every ```file block as an attachment and rewrite the message.

    Returns the (possibly unchanged) content. Never raises.
    """
    matches = list(_FILE_BLOCK.finditer(content))
    if not matches:
        return content
    try:
        if project_id is None:
            lookup = getattr(uow.conversations, "project_id_for_message", None)
            project_id = await lookup(message_id) if lookup else None
        if not project_id:
            return content  # attachments need a project to hang on
        if blob_store is None:
            from proseforge.infrastructure.blob.local import LocalBlobStore
            from proseforge.settings import get_settings

            blob_store = LocalBlobStore(get_settings().blob_root)
        rewritten = content
        for index, match in enumerate(matches, start=1):
            body = match.group("body")
            data = body.encode("utf-8")
            if len(data) > MAX_FILE_BYTES:
                continue  # oversized: keep the original block verbatim
            filename = sanitize_filename(match.group("name"), index)
            storage_key = await blob_store.put(data=data, media_type="text/plain")
            digest = hashlib.sha256(data).hexdigest()
            attachment = await uow.attachments.add(project_id, filename, digest, storage_key, message_id=message_id)
            link = f"**下载链接：**[{filename}](/api/v1/files/{attachment.id}/download)"
            preview = f"```{_preview_lang(filename)}\n{body}```"
            rewritten = rewritten.replace(match.group(0), f"{link}\n\n{preview}", 1)
        return rewritten
    except Exception:
        return content  # blob/DB failures must not fail the generation
