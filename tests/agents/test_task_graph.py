import pytest

from proseforge.application.agents.validate_graph import validate_graph
from proseforge.domain.agents.task_graph import AgentTaskSpec, TaskGraph


def test_graph_validates_order_and_rejects_cycles():
    graph = TaskGraph(1, (AgentTaskSpec("a", "chief_planner"), AgentTaskSpec("b", "scene_writer", ("a",))))
    assert validate_graph(graph) == ("a", "b")
    cyclic = TaskGraph(1, (AgentTaskSpec("a", "chief_planner", ("b",)), AgentTaskSpec("b", "scene_writer", ("a",))))
    with pytest.raises(ValueError, match="cycle"): validate_graph(cyclic)
