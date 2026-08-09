"""V2 Workflow Studio 任务 handler（celery-free，V2-008）。

签名与 tasks.py 一致：``async handler(payload: dict[str, object]) -> object``。
模块级不 import celery/sqlalchemy——重型依赖全部在函数体内惰性 import；
celery_app.py 只做薄封装，native profile 经 tasks.HANDLERS 复用
execute_v2_run。recover_expired_v2 仅由 celery beat 驱动（重排走 broker），
native profile 的 v2 恢复由 API lifespan 的 maintenance_tick 直接执行。
"""

from __future__ import annotations

EXECUTE_V2_RUN_TASK = "proseforge.workflows.execute_v2_run"
RECOVER_V2_TASK = "proseforge.workflows.recover_expired_v2"


async def execute_v2_run(payload: dict[str, object]) -> str:
    from proseforge.application.workflows.executor import WorkflowRunExecutor
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.settings import get_settings

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    run_id = str(payload["run_id"])
    try:
        executor = WorkflowRunExecutor(lambda: SqlAlchemyUnitOfWork(session_factory))
        delivery_id = str(payload.get("task_id") or __import__("uuid").uuid4().hex)
        return await executor.execute(run_id, f"celery:v2:{run_id}:{delivery_id}")
    finally:
        await engine.dispose()


async def recover_expired_v2(payload: dict[str, object]) -> dict[str, int]:
    """租约过期的节点重排为 PENDING、run 回到 QUEUED，然后为每个待执行的
    definition run 重新入队 execute_v2_run。重复入队由执行器的 run 租约去重。"""
    from proseforge.application.workflows.recover_run import (
        queued_definition_run_ids,
        recover_expired_workflow_nodes,
    )
    from proseforge.infrastructure.database.session import (
        create_engine_and_sessionmaker,
    )
    from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from proseforge.settings import get_settings

    del payload
    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            recovered = await recover_expired_workflow_nodes(uow)
            run_ids = await queued_definition_run_ids(uow)
            await uow.commit()
        enqueued = 0
        if run_ids:
            from celery import current_app

            for run_id in run_ids:
                current_app.send_task(EXECUTE_V2_RUN_TASK, args=[{"run_id": run_id}])
                enqueued += 1
        return {"recovered": recovered, "enqueued": enqueued}
    finally:
        await engine.dispose()
