"""Regression tests for the v1 workflow SSE stream (`/api/v1/workflows/{id}/events`).

The API batch fixtures require a live PostgreSQL instance, so these tests drive
the route's streaming body directly with in-memory fakes — the same no-DB
approach as test_sse_reconnect.py.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock

import pytest

from proseforge.api.routes.workflows import workflow_events
from proseforge.application.auth.service import AuthUser


class _FakeUow:
    """Stands in for the unit of work: the run exists when the stream opens."""

    def __init__(self, run: object) -> None:
        self.workflows = SimpleNamespace(get_owned=AsyncMock(return_value=run))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Serves no event rows and a scripted sequence of run statuses."""

    def __init__(self, statuses: list[str | None]) -> None:
        self._statuses = statuses

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def close(self) -> None:
        """Route teardown closes the session explicitly (cancellation-safe)."""
        return

    async def scalars(self, _statement: object) -> SimpleNamespace:
        return SimpleNamespace(all=list)

    async def scalar(self, _statement: object) -> str | None:
        return self._statuses.pop(0)


class _FakeRequest:
    def __init__(self, statuses: list[str | None]) -> None:
        self.headers: dict[str, str] = {}
        self.app = SimpleNamespace(
            state=SimpleNamespace(session_factory=lambda: _FakeSession(statuses))
        )

    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_workflow_events_stream_terminates_when_run_is_deleted_mid_stream():
    # First poll: run still RUNNING; second poll: run row gone (deleted).
    request = _FakeRequest(statuses=["RUNNING", None])

    response = await workflow_events(
        workflow_id="run-1",
        request=request,
        user=AuthUser(id="u1", email="u1@example.local"),
        uow=_FakeUow(run=object()),
    )

    chunks: list[bytes] = []

    async def collect() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    # Without the fix the generator spins until the client disconnects;
    # the timeout turns that regression into a failure instead of a hang.
    await asyncio.wait_for(collect(), timeout=10)

    payload = b"".join(chunks).decode()
    assert "event: run.deleted" in payload
    assert '"status":"DELETED"' in payload
