from datetime import UTC, datetime, timedelta

from proseforge.domain.workflow.state import WorkflowRun


def test_expired_lease_enters_recovery_and_releases_owner():
    now = datetime.now(UTC)
    run = WorkflowRun("run-1")
    run.state.transition("QUEUED")
    run.state.transition("RUNNING")
    assert run.acquire_lease("worker-1", ttl_seconds=1, now=now)
    run.recover_if_expired(now + timedelta(seconds=2))
    assert run.state.status == "RECOVERING"
    assert run.lease_owner is None
