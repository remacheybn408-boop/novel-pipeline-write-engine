import pytest

from proseforge.domain.workflow.state import InvalidWorkflowTransition, WorkflowState


def test_completed_cannot_restart():
    state = WorkflowState("COMPLETED")
    with pytest.raises(InvalidWorkflowTransition):
        state.transition("RUNNING")
