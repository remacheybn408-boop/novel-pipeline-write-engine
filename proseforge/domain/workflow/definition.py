from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NodeKind = Literal["intake", "plan", "write", "review", "rewrite", "export"]


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    kind: NodeKind
    title: str


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    revision: int
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[tuple[str, str], ...]

    def validate(self) -> None:
        ids = {node.id for node in self.nodes}
        if any(source not in ids or target not in ids for source, target in self.edges): raise ValueError("workflow edge references unknown node")
        if any(source == target for source, target in self.edges): raise ValueError("workflow self-loop is not allowed")
