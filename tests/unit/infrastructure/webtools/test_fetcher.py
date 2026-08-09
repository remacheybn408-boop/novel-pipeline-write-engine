"""Offline tests for SafeFetcher: SSRF guard, retry policy, circuit breaker."""

from __future__ import annotations

import httpx
import pytest

import proseforge.infrastructure.webtools.fetcher as fetcher_module
from proseforge.infrastructure.webtools import SafeFetcher


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(fetcher_module, "RETRY_BACKOFF_SECONDS", (0.01, 0.01))


def _fetcher(handler, **kwargs) -> SafeFetcher:
    kwargs.setdefault("rate_seconds_per_domain", 0)
    return SafeFetcher(transport=httpx.MockTransport(handler), **kwargs)


@pytest.mark.asyncio
async def test_ssrf_blocked_before_any_request():
    calls = []
    fetcher = _fetcher(lambda request: calls.append(1) or httpx.Response(200))
    outcome = await fetcher.fetch_html("http://127.0.0.1/admin")
    assert outcome.error_kind == "ssrf" and not calls


@pytest.mark.asyncio
async def test_redirect_to_private_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            raise AssertionError("private hop must never be requested")
        return httpx.Response(302, headers={"location": "http://127.0.0.1/x"})

    outcome = await _fetcher(handler).fetch_html("https://example.com/")
    assert outcome.error_kind == "ssrf" and "blocked" in (outcome.error or "")


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500)
        return httpx.Response(200, text="<html>ok</html>")

    outcome = await _fetcher(handler).fetch_html("https://example.com/")
    assert outcome.html == "<html>ok</html>" and len(attempts) == 3  # 1 + 2 retries


@pytest.mark.asyncio
async def test_no_retry_on_404():
    attempts = []
    outcome = await _fetcher(lambda request: attempts.append(1) or httpx.Response(404)).fetch_html("https://example.com/")
    assert outcome.status_code == 404 and len(attempts) == 1


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_three_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    fetcher = _fetcher(handler)
    for _ in range(3):
        outcome = await fetcher.fetch_html(f"https://example.com/{_}")
        assert outcome.error_kind == "upstream"
    outcome = await fetcher.fetch_html("https://example.com/again")
    assert outcome.error_kind == "circuit"


@pytest.mark.asyncio
async def test_ttl_cache_hit_skips_http():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="<html>cached</html>")

    fetcher = _fetcher(handler)
    first = await fetcher.fetch_html("https://example.com/")
    second = await fetcher.fetch_html("https://example.com/")
    assert first.html == second.html == "<html>cached</html>"
    assert second.cache_hit is True and len(calls) == 1
