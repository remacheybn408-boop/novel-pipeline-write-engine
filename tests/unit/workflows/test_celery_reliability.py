from proseforge.workflows.celery_app import celery


def test_worker_lost_tasks_are_requeued_after_restart():
    assert celery.conf.task_acks_late is True
    assert celery.conf.task_reject_on_worker_lost is True
    assert celery.conf.worker_prefetch_multiplier == 1
