"""Tests for cancellation-resilient session teardown and pool log filtering.

Client aborts (SSE disconnect / page close) cancel the request task mid-
teardown; the helpers in ``infrastructure.database.session`` must still
complete the connection checkin, and the pool log filter must demote the
residual terminate/GC noise without hiding genuine pool errors.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from proseforge.infrastructure.database.session import (
    _TEARDOWN_ATTEMPTS,
    _PoolTeardownNoiseFilter,
    close_session,
    rollback_session,
)
from proseforge.infrastructure.database.uow import SqlAlchemyUnitOfWork


class _FlakySession:
    """Session stand-in whose teardown fails with CancelledError N times."""

    def __init__(self, rollback_failures: int = 0, close_failures: int = 0):
        self.rollback_calls = 0
        self.close_calls = 0
        self._rollback_failures = rollback_failures
        self._close_failures = close_failures

    async def rollback(self) -> None:
        self.rollback_calls += 1
        if self._rollback_failures > 0:
            self._rollback_failures -= 1
            raise asyncio.CancelledError

    async def close(self) -> None:
        self.close_calls += 1
        if self._close_failures > 0:
            self._close_failures -= 1
            raise asyncio.CancelledError


def _pool_record(message: str, *, exc_info=None, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool", level, __file__, 0, message, None, exc_info
    )


@pytest.mark.asyncio
async def test_close_session_retries_after_cancellation() -> None:
    session = _FlakySession(close_failures=1)
    await close_session(session)  # type: ignore[arg-type]
    assert session.close_calls == 2


@pytest.mark.asyncio
async def test_close_session_gives_up_and_propagates_cancellation() -> None:
    session = _FlakySession(close_failures=_TEARDOWN_ATTEMPTS + 1)
    with pytest.raises(asyncio.CancelledError):
        await close_session(session)  # type: ignore[arg-type]
    assert session.close_calls == _TEARDOWN_ATTEMPTS


@pytest.mark.asyncio
async def test_rollback_session_retries_after_cancellation() -> None:
    session = _FlakySession(rollback_failures=2)
    await rollback_session(session)  # type: ignore[arg-type]
    assert session.rollback_calls == 3


@pytest.mark.asyncio
async def test_uow_aexit_completes_teardown_under_cancellation() -> None:
    uow = SqlAlchemyUnitOfWork(session_factory=None)  # type: ignore[arg-type]
    session = _FlakySession(rollback_failures=1, close_failures=1)
    uow.session = session  # type: ignore[assignment]
    # Simulate a request cancelled while the uow block was active.
    await uow.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None)
    assert session.rollback_calls == 2
    assert session.close_calls == 2
    assert uow.session is None


def test_filter_demotes_gc_checkin_message() -> None:
    record = _pool_record("The garbage collector is trying to clean up non-checked-in connection <x>")
    assert _PoolTeardownNoiseFilter().filter(record) is False


def test_filter_demotes_cancelled_terminate() -> None:
    try:
        raise asyncio.CancelledError
    except asyncio.CancelledError:
        exc_info = sys.exc_info()
    assert _PoolTeardownNoiseFilter().filter(_pool_record("Exception terminating connection <x>", exc_info=exc_info)) is False
    assert _PoolTeardownNoiseFilter().filter(_pool_record("Exception during reset or similar", exc_info=exc_info)) is False


def test_filter_keeps_genuine_terminate_error() -> None:
    try:
        raise RuntimeError("connection reset by peer")
    except RuntimeError:
        exc_info = sys.exc_info()
    assert _PoolTeardownNoiseFilter().filter(_pool_record("Exception terminating connection <x>", exc_info=exc_info)) is True


def test_filter_keeps_other_pool_errors() -> None:
    assert _PoolTeardownNoiseFilter().filter(_pool_record("some other pool failure")) is True
