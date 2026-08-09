from __future__ import annotations

from datetime import UTC, datetime

from proseforge.domain.revision.proposal import RevisionProposal


def reject_proposal(proposal: RevisionProposal) -> RevisionProposal:
    return proposal.transition("REJECTED")


async def reject_persisted_proposal(*, uow, proposal_id: str, user_id: str):
    row = await uow.revisions.get_owned_for_update(proposal_id, user_id)
    if row is None:
        raise LookupError("proposal not found")
    if row.status == "REJECTED":
        return row
    if row.status != "PROPOSED":
        raise ValueError("proposal is not rejectable")
    row.status = "REJECTED"
    row.decided_at = datetime.now(UTC)
    row.updated_at = row.decided_at
    return row
