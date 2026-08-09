from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from proseforge.infrastructure.database.sqlite import create_sqlite_engine
from proseforge.runtime.profile import RuntimeProfile, capabilities_for

# How many times a teardown step is retried when the request cancellation
# scope keeps interrupting it. Teardown is idempotent, so retrying is safe;
# giving up afterwards lets the cancellation propagate (the pool teardown
# log filter below then keeps the residual noise out of ERROR).
_TEARDOWN_ATTEMPTS = 3


async def _run_teardown_step(step: Callable[[], Awaitable[None]]) -> None:
    """Run an idempotent session teardown step to completion.

    When a client aborts (SSE disconnect, page close), starlette cancels the
    request task and the CancelledError can land inside rollback()/close(),
    leaving the pooled connection half-checked-in. Both steps are idempotent,
    so re-invoking them after an interrupted attempt completes the checkin.
    The cancellation itself is not swallowed permanently: if teardown is
    interrupted on every attempt, the last CancelledError propagates.
    """
    for attempt in range(_TEARDOWN_ATTEMPTS):
        try:
            await step()
            return
        except asyncio.CancelledError:
            if attempt == _TEARDOWN_ATTEMPTS - 1:
                raise


async def rollback_session(session: AsyncSession) -> None:
    """Roll back a session, resilient to cancellation during teardown."""
    await _run_teardown_step(session.rollback)


async def close_session(session: AsyncSession) -> None:
    """Close a session (return the connection), resilient to cancellation."""
    await _run_teardown_step(session.close)


_POOL_LOGGER_NAME = "sqlalchemy.pool.impl.AsyncAdaptedQueuePool"
_GC_CHECKIN_MESSAGE = "The garbage collector is trying to clean up non-checked-in connection"
_TERMINATE_MESSAGES = ("Exception terminating connection", "Exception during reset")


class _PoolTeardownNoiseFilter(logging.Filter):
    """Demote pool ERROR records that are a symptom of client disconnects.

    Two record families are matched (nothing else on this logger is touched):
    - terminate/reset failures whose exc_info is a CancelledError — asyncpg
      connection teardown interrupted by the request cancellation scope;
    - the GC "non-checked-in connection" message — the checkin interrupted
      by the same cancellation, with the fairy later reaped by the GC.
    Matched records are re-emitted at DEBUG and dropped from ERROR so a real
    terminate failure (any other exception type) stays visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        message = record.getMessage()
        if message.startswith(_GC_CHECKIN_MESSAGE):
            return self._demote(record)
        if message.startswith(_TERMINATE_MESSAGES):
            exc_type = record.exc_info[0] if record.exc_info else None
            if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                return self._demote(record)
        return True

    @staticmethod
    def _demote(record: logging.LogRecord) -> bool:
        logging.getLogger(record.name).debug(
            "pool teardown interrupted by request cancellation: %s", record.getMessage()
        )
        return False


def install_pool_teardown_log_filter() -> None:
    """Attach the cancellation-noise filter to the asyncpg pool logger (idempotent)."""
    pool_logger = logging.getLogger(_POOL_LOGGER_NAME)
    if any(isinstance(existing, _PoolTeardownNoiseFilter) for existing in pool_logger.filters):
        return
    pool_logger.addFilter(_PoolTeardownNoiseFilter())


def create_engine_and_sessionmaker(settings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    profile = RuntimeProfile(settings.runtime_profile)
    if capabilities_for(profile).database == "sqlite":
        engine = _create_native_engine(settings)
    else:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        install_pool_teardown_log_filter()
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


def _create_native_engine(settings) -> AsyncEngine:
    database = make_url(settings.database_url).database
    if not database or database == ":memory:":
        raise ValueError(
            "native runtime profile requires a file-backed SQLite database_url"
        )
    return create_sqlite_engine(Path(database))
