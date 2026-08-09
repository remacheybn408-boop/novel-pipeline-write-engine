"""Offline SearchService tests: failover, TTL cache, SSRF-safe web_fetch."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from proseforge.infrastructure.search import EngineUnavailable, SearchService

FIXTURES = Path(__file__).parent / "fixtures"
BING_HTML = (FIXTURES / "bing.html").read_text(encoding="utf-8")
PAGE_HTML = "<html><body><nav>menu</nav><main><p>Hello   world body</p></main><script>evil()</script></body></html>"


class RequestCounter:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return self.handler(request)


def _engine_router(request: httpx.Request) -> httpx.Response:
    # duckduckgo down, bing serving the fixture page.
    if "duckduckgo.com" in request.url.host:
        return httpx.Response(502, text="bad gateway")
    if "bing.com" in request.url.host:
        return httpx.Response(200, text=BING_HTML)
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_failover_to_second_engine():
    counter = RequestCounter(_engine_router)
    service = SearchService(engines=("duckduckgo", "bing"), transport=httpx.MockTransport(counter))
    results = await service.web_search("test query", 5)
    assert results[0].url == "https://example.com/bing-1"
    assert all(r.engine == "bing" for r in results)
    assert any("duckduckgo" in url for url in counter.requests)  # first engine was tried


@pytest.mark.asyncio
async def test_all_engines_down_raises():
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    service = SearchService(engines=("duckduckgo", "bing"), transport=transport)
    with pytest.raises(EngineUnavailable):
        await service.web_search("test query", 5)


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    counter = RequestCounter(_engine_router)
    service = SearchService(engines=("bing",), transport=httpx.MockTransport(counter))
    first = await service.web_search("Test Query", 5)
    second = await service.web_search("  test query  ", 5)  # same cache key
    assert first == second
    assert len(counter.requests) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_results_deduped_by_url():
    service = SearchService(engines=("bing",), transport=httpx.MockTransport(_engine_router))
    results = await service.web_search("test query", 5)
    urls = [r.url for r in results]
    assert len(urls) == len(set(urls))


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_url():
    service = SearchService()
    text, error = await service.web_fetch("http://127.0.0.1/admin", 6000)
    assert text is None
    assert error is not None and "blocked" in error


@pytest.mark.asyncio
async def test_web_fetch_blocks_redirect_to_private():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})

    counter = RequestCounter(handler)
    service = SearchService(transport=httpx.MockTransport(counter))
    text, error = await service.web_fetch("https://example.com/start", 6000)
    assert text is None
    assert error is not None and "blocked" in error
    assert len(counter.requests) == 1  # the private hop was never requested


@pytest.mark.asyncio
async def test_web_fetch_follows_public_redirect_and_strips_boilerplate():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.org/page"})
        return httpx.Response(200, text=PAGE_HTML)

    service = SearchService(transport=httpx.MockTransport(handler))
    text, error = await service.web_fetch("https://example.com/start", 6000)
    assert error is None
    assert text == "Hello world body"  # nav/script stripped, whitespace collapsed


@pytest.mark.asyncio
async def test_web_fetch_truncates_to_max_chars():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=f"<html><body><p>{'x' * 100}</p></body></html>"))
    service = SearchService(transport=transport)
    text, error = await service.web_fetch("https://example.com/", 10)
    assert error is None
    assert text is not None and len(text) == 10
