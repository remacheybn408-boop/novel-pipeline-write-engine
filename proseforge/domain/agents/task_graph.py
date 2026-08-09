from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentTaskSpec:
    id: str
    role: str
    depends_on: tuple[str, ...] = ()
    input_artifact_types: tuple[str, ...] = ()
    output_artifact_type: str = "report"
    max_attempts: int = 3
    timeout_seconds: int = 300
    token_budget: int = 1
    permission_profile: str = "default"

@dataclass(frozen=True)
class TaskGraph:
    revision: int
    tasks: tuple[AgentTaskSpec, ...]

    def topological_order(self) -> tuple[str, ...]:
        ids = {task.id for task in self.tasks}; adjacency = {task.id: set() for task in self.tasks}; indegree = {task.id: 0 for task in self.tasks}
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency not in ids: raise ValueError("missing task dependency")
                adjacency[dependency].add(task.id); indegree[task.id] += 1
        ready = sorted(task_id for task_id, degree in indegree.items() if degree == 0); order = []
        while ready:
            current = ready.pop(0); order.append(current)
            for child in sorted(adjacency[current]):
                indegree[child] -= 1
                if indegree[child] == 0: ready.append(child)
        if len(order) != len(self.tasks): raise ValueError("task graph contains a cycle")
        return tuple(order)
