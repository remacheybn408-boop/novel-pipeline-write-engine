from proseforge.domain.agents.roles import AgentRole
from proseforge.domain.agents.task_graph import TaskGraph


def validate_graph(graph: TaskGraph) -> tuple[str, ...]:
    if len(graph.tasks) > 64: raise ValueError("task graph exceeds node limit")
    if len(graph.tasks) == 0: raise ValueError("task graph must contain at least one task")
    if len({task.id for task in graph.tasks}) != len(graph.tasks): raise ValueError("duplicate task id")
    for task in graph.tasks:
        if task.role not in {role.value for role in AgentRole}: raise ValueError("unknown role")
        if task.token_budget <= 0: raise ValueError("task token budget must be positive")
        if len(task.depends_on) > 8: raise ValueError("task fan-in exceeds limit")
    order = graph.topological_order()
    children = {task.id: 0 for task in graph.tasks}
    for task in graph.tasks:
        for dependency in task.depends_on:
            children[dependency] += 1
    if max(children.values(), default=0) > 8: raise ValueError("task fanout exceeds limit")
    depths = {task_id: 0 for task_id in order}
    by_id = {task.id: task for task in graph.tasks}
    for task_id in order:
        depths[task_id] = max((depths[parent] + 1 for parent in by_id[task_id].depends_on), default=0)
    if max(depths.values(), default=0) > 8: raise ValueError("task graph exceeds depth limit")
    return order
