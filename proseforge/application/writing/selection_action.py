"""Create revision proposals from a verified manuscript selection.

The generator in this module is deliberately small and deterministic for the
first editor slice.  Its boundary is the important part: later model-backed
generation can replace ``_candidate_after_text`` without ever granting a
selection action permission to create a chapter version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

SelectionActionName = Literal[
    "continue",
    "expand",
    "shorten",
    "rewrite",
    "change-tone",
    "review",
]


class SelectionActionConflict(Exception):
    """The editor selection no longer describes the active chapter version."""

    def __init__(self, code: str, *, current_version_id: str | None = None):
        self.code = code
        self.current_version_id = current_version_id
        super().__init__(code)


class SelectionActionValidationError(Exception):
    """The selection is structurally invalid for the active document."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SelectionActionRequest:
    action: SelectionActionName
    start: int
    end: int
    selected_text_hash: str
    base_version_id: str
    params: dict[str, object]


@dataclass(frozen=True)
class SelectionActionResult:
    proposal_ids: tuple[str, ...]
    review_id: str | None = None


def _candidate_count(request: SelectionActionRequest) -> int:
    if request.action != "continue":
        return 1
    candidates = request.params.get("candidates", 1)
    if isinstance(candidates, bool) or not isinstance(candidates, int) or not 1 <= candidates <= 3:
        raise SelectionActionValidationError("INVALID_CANDIDATE_COUNT")
    return candidates


def _candidate_after_text(content: str, request: SelectionActionRequest, candidate: int) -> str:
    """Return a replaceable placeholder proposal while generation is unimplemented.

    Storing a complete proposed document keeps compatibility with the current
    revision repository.  The V2-007 hunk engine can replace this formatter
    without changing the route's concurrency and ownership checks.
    """

    selected = content[request.start:request.end]
    if request.action == "continue":
        generated = f"\n\n[Continue candidate {candidate}: {selected}]"
        return f"{content[:request.end]}{generated}{content[request.end:]}"
    if request.action == "expand":
        ratio = request.params.get("ratio", 1.5)
        generated = f"{selected}\n\n[Expanded x{ratio}]"
    elif request.action == "shorten":
        ratio = request.params.get("ratio", 0.7)
        generated = f"[Shortened x{ratio}: {selected}]"
    elif request.action == "rewrite":
        generated = f"[Rewrite: {selected}]"
    elif request.action == "change-tone":
        register = request.params.get("register", "default")
        generated = f"[Tone {register}: {selected}]"
    else:  # review is represented as a proposal until V2-007 adds review reports.
        generated = f"[Review requested: {selected}]"
    return f"{content[:request.start]}{generated}{content[request.end:]}"


async def create_selection_action_proposals(
    *,
    uow,
    owner_id: str,
    chapter_id: str,
    request: SelectionActionRequest,
) -> SelectionActionResult:
    """Verify a selection against the active version and create proposals only."""

    chapter = await uow.chapters.get_owned(chapter_id, owner_id)
    if chapter is None:
        raise LookupError("chapter not found")

    active = await uow.revisions.current_version(chapter_id, owner_id)
    if active is None or active.id != request.base_version_id:
        raise SelectionActionConflict(
            "BASE_VERSION_CONFLICT",
            current_version_id=active.id if active is not None else None,
        )

    content = active.content
    if not 0 <= request.start < request.end <= len(content):
        raise SelectionActionValidationError("INVALID_SELECTION_RANGE")
    actual_hash = hashlib.sha256(content[request.start:request.end].encode("utf-8")).hexdigest()
    if actual_hash != request.selected_text_hash:
        raise SelectionActionConflict("SELECTION_HASH_CONFLICT", current_version_id=active.id)

    rationale = json.dumps(
        {"source": "selection-action", "action": request.action, "params": request.params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if request.action == "review":
        report = await uow.revisions.create_review(
            project_id=chapter.project_id,
            scope="selection",
            subject_type="chapter",
            subject_id=chapter_id,
            findings=[],
            scores={},
            model_snapshot={"source": "selection-action"},
        )
        return SelectionActionResult((), report.id)
    proposal_ids: list[str] = []
    for candidate in range(1, _candidate_count(request) + 1):
        proposal = await uow.revisions.create(
            chapter_id=chapter_id,
            base_version_id=active.id,
            before=content,
            after=_candidate_after_text(content, request, candidate),
            rationale=rationale,
        )
        proposal_ids.append(proposal.id)
    return SelectionActionResult(tuple(proposal_ids))
