"""Full-text loader for upstream artifacts (review-stage prompts).

Executor-side artifact snapshots only carry a 200-char preview — enough
for writers, useless for reviewers ("content missing" in stress tests).
This module fetches the persisted payloads and extracts reviewable text:
SceneDraft-like payloads yield their content; everything else falls back
to a JSON digest. Oversized texts are elided head 70% / tail 20% with a
middle marker so both ends stay visible to the reviewer.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from proseforge.infrastructure.database.models.agents import AgentArtifactModel

_HEAD_RATIO = 0.7
_TAIL_RATIO = 0.2


def elide_middle(text: str, max_chars: int) -> str:
    """Keep head 70% + tail 20%, dropping the middle with a marker."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * _HEAD_RATIO)
    tail = int(max_chars * _TAIL_RATIO)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…（中段省略 {omitted} 字）…\n{text[-tail:]}"


def extract_artifact_text(payload: object) -> str:
    """Reviewable text from an artifact payload (dict or JSON string)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return payload
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    # Non-scene payloads (outline, candidate lists, reports): JSON digest.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


async def load_artifact_texts(context, *, max_chars_per_artifact: int) -> dict[str, str]:
    """{artifact_id: elided full text} for context["artifacts"].

    Reads via context["uow_factory"] in one short transaction; artifacts
    missing from the DB simply yield no entry.
    """
    artifacts = [item for item in context.get("artifacts", []) if isinstance(item, dict)]
    ids = [str(item.get("id", "")) for item in artifacts if item.get("id")]
    if not ids:
        return {}
    uow_factory = context["uow_factory"]
    async with uow_factory() as uow:
        rows = await uow.session.scalars(select(AgentArtifactModel).where(AgentArtifactModel.id.in_(ids)))
        texts: dict[str, str] = {}
        for row in rows:
            text = extract_artifact_text(row.payload)
            if text:
                texts[row.id] = elide_middle(text, max_chars_per_artifact)
        return texts
