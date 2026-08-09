from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from proseforge.domain.revision.proposal import RevisionProposal, content_hash


class ApprovalConflict(Exception):
    def __init__(self, code: str, current_version_id: str | None = None):
        self.code = code
        self.current_version_id = current_version_id
        super().__init__(code)


class ApprovalBlocked(Exception):
    pass


@dataclass(frozen=True)
class ApprovalResult:
    proposal: object
    version: object | None
    replayed: bool
    retrieval_job_id: str | None = None
    summarize_job_id: str | None = None


def apply_hunks(base_text: str, hunks: list[dict[str, object]], accepted_hunks: set[int] | None = None) -> str:
    selected = [(index, hunk) for index, hunk in enumerate(hunks) if accepted_hunks is None or index in accepted_hunks]
    output = base_text
    for _, hunk in sorted(selected, key=lambda item: int(item[1].get("start", 0)), reverse=True):
        start, end = int(hunk["start"]), int(hunk["end"])
        if not 0 <= start <= end <= len(base_text):
            raise ApprovalConflict("INVALID_HUNK")
        output = output[:start] + str(hunk.get("replacement", "")) + output[end:]
    return output


async def approve_persisted_proposal(*, uow, proposal_id: str, user_id: str, idempotency_key: str | None, accept_hunks: list[int] | None = None) -> ApprovalResult:
    row = await uow.revisions.get_owned_for_update(proposal_id, user_id)
    if row is None:
        raise LookupError("proposal not found")
    if row.status == "VERSION_CREATED":
        version = await uow.revisions.current_version(row.chapter_id, user_id)
        return ApprovalResult(row, version, True)
    if row.status != "PROPOSED":
        raise ApprovalConflict("PROPOSAL_NOT_APPROVABLE")
    if row.guard_status == "blocked":
        raise ApprovalBlocked("proposal has blocking review findings")
    current = await uow.revisions.current_version(row.chapter_id, user_id)
    if current is None or current.id != row.base_version_id or current.content_hash != row.before_hash:
        raise ApprovalConflict("REVISION_BASE_CONFLICT", current.id if current else None)
    hunks = json.loads(row.hunks_json)
    text = apply_hunks(current.content, hunks, set(accept_hunks) if accept_hunks is not None else None)
    version = await uow.chapters.append_version(chapter_id=row.chapter_id, content=text)
    await uow.chapters.set_active_version(row.chapter_id, version.id)
    row.status = "VERSION_CREATED"
    row.idempotency_key = idempotency_key
    row.decided_at = datetime.now(UTC)
    row.updated_at = row.decided_at
    # Narrative RAG: queue re-indexing of the new active version in the same
    # transaction; the route enqueues the worker task after commit.
    retrieval_job_id = None
    summarize_job_id = None
    chapter = await uow.chapters.get_owned(row.chapter_id, user_id)
    if chapter is not None:
        job = await uow.retrieval.enqueue_job(
            project_id=chapter.project_id, job_type="index_chapter", source_type="chapter", source_id=row.chapter_id
        )
        retrieval_job_id = job.id
        # Chapter summary + character extraction rides the same approval
        # funnel, keyed to the immutable version for idempotency.
        summarize_job = await uow.retrieval.enqueue_job(
            project_id=chapter.project_id, job_type="summarize_chapter", source_type="chapter_version", source_id=version.id
        )
        summarize_job_id = summarize_job.id
    return ApprovalResult(row, version, False, retrieval_job_id, summarize_job_id)


def approve_proposal(proposal: RevisionProposal, current_content: str) -> RevisionProposal:
    """Compatibility wrapper for the unit-level domain API."""
    return proposal.approve(content_hash(current_content))
