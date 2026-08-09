from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

ProposalStatus = Literal["DRAFT", "GENERATED", "REVIEWED", "PROPOSED", "APPROVED", "VERSION_CREATED", "REJECTED", "EXPIRED"]
_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"GENERATED"}, "GENERATED": {"REVIEWED", "PROPOSED", "EXPIRED"},
    "REVIEWED": {"PROPOSED", "REJECTED", "EXPIRED"}, "PROPOSED": {"APPROVED", "REJECTED", "EXPIRED"},
    "APPROVED": {"VERSION_CREATED"}, "VERSION_CREATED": set(), "REJECTED": set(), "EXPIRED": set(),
}


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class RevisionProposal:
    base_version_id: str
    before_hash: str
    after_text: str
    rationale: str
    status: ProposalStatus = "PROPOSED"

    @property
    def after_hash(self) -> str:
        return content_hash(self.after_text)

    def transition(self, next_status: ProposalStatus) -> RevisionProposal:
        if next_status not in _TRANSITIONS[self.status]:
            raise ValueError(f"invalid proposal transition: {self.status} -> {next_status}")
        return RevisionProposal(self.base_version_id, self.before_hash, self.after_text, self.rationale, next_status)

    def approve(self, current_hash: str) -> RevisionProposal:
        if current_hash != self.before_hash:
            raise ValueError("stale proposal base")
        return self.transition("APPROVED")
