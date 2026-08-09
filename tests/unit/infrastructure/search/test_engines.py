"""Offline parser tests for the three search engine adapters.

Fixtures are hand-built synthetic HTML (see the comment inside each fixture);
selectors must be revalidated against live pages after deploy.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from proseforge.infrastructure.search.engines import (
    BaiduEngine,
    BingEngine,
    BraveEngine,
    DuckDuckGoEngine,
    EcosiaEngine,
    EngineUnavailable,
    GoogleEngine,
    MojeekEngine,
    StartpageEngine,
    YahooEngine,
)

FIXTURES = Path(__file__).parent / "fixtures"
# New-engine fixtures live in the shared tests/fixtures tree (task convention).
FIXTURES_V2 = Path(__file__).parents[3] / "fixtures" / "search"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_duckduckgo_parse_unwraps_uddg_and_collapses_whitespace():
    results = DuckDuckGoEngine()._parse(_load("duckduckgo.html"), max_results=5)
    assert len(results) == 2  # the empty-href ad node is skipped
    assert results[0].title == "Example News One"
    assert results[0].url == "https://example.com/news-1"  # uddg redirect unwrapped
    assert results[0].snippet == "First snippet with odd spacing"
    assert results[0].engine == "duckduckgo"
    assert results[1].url == "https://direct.example.org/page"


def test_duckduckgo_parse_respects_max_results():
    results = DuckDuckGoEngine()._parse(_load("duckduckgo.html"), max_results=1)
    assert len(results) == 1


def test_bing_parse():
    results = BingEngine()._parse(_load("bing.html"), max_results=5)
    assert [r.url for r in results] == ["https://example.com/bing-1", "https://example.net/bing-2"]
    assert results[0].title == "Bing Result One"
    assert results[0].snippet == "Bing snippet one"
    assert results[0].engine == "bing"


def test_baidu_parse_keeps_redirect_hops():
    results = BaiduEngine()._parse(_load("baidu.html"), max_results=5)
    assert len(results) == 2
    assert results[0].title == "百度结果一"
    assert results[0].url == "https://www.baidu.com/link?url=abc123def"  # kept as-is
    assert results[0].snippet == "百度 摘要一"
    assert results[0].engine == "baidu"


@pytest.mark.asyncio
async def test_engine_http_error_raises_unavailable():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="blocked"))
    with pytest.raises(EngineUnavailable, match="HTTP error"):
        await BingEngine(transport=transport).search("query", 5)


@pytest.mark.asyncio
async def test_engine_unparseable_page_raises_unavailable():
    # A 200 page with no result nodes must NOT become an empty success list.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html><body>captcha</body></html>"))
    with pytest.raises(EngineUnavailable, match="no result nodes"):
        await DuckDuckGoEngine(transport=transport).search("query", 5)


# --- New engines (selectors per SearXNG definitions; fixtures are synthetic) ---


def _load_v2(name: str) -> str:
    return (FIXTURES_V2 / name).read_text(encoding="utf-8")


def test_google_parse_unwraps_redirector_and_skips_classed_anchors():
    results = GoogleEngine()._parse(_load_v2("google.html"), max_results=5)
    assert len(results) == 2  # the data-ved anchor WITH a class is skipped
    assert results[0].title == "Google Result One"
    assert results[0].url == "https://example.com/google-1"  # /url?q= redirector unwrapped
    assert results[0].snippet == "Google snippet one"
    assert results[0].engine == "google"
    assert results[1].url == "https://example.org/google-2"


@pytest.mark.asyncio
async def test_google_captcha_page_raises_unavailable():
    body = "<html><body>Our systems have detected unusual traffic from your computer network.</body></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    with pytest.raises(EngineUnavailable, match="captcha"):
        await GoogleEngine(transport=transport).search("query", 5)


def test_yahoo_parse_unwraps_tracking_hops():
    results = YahooEngine()._parse(_load_v2("yahoo.html"), max_results=5)
    assert len(results) == 2
    assert results[0].title == "Yahoo Result One"  # from aria-label
    assert results[0].url == "https://example.com/yahoo-1"  # r.search.yahoo.com hop unwrapped
    assert results[0].snippet == "Yahoo snippet one"
    assert results[0].engine == "yahoo"
    assert results[1].url == "https://example.org/yahoo-2"  # direct link kept as-is


def test_brave_parse_skips_relative_href_ads():
    results = BraveEngine()._parse(_load_v2("brave.html"), max_results=5)
    assert [r.url for r in results] == ["https://example.com/brave-1", "https://example.org/brave-2"]
    assert results[0].title == "Brave Result One"
    assert results[0].snippet == "Brave snippet one"
    assert results[0].engine == "brave"


def test_mojeek_parse():
    results = MojeekEngine()._parse(_load_v2("mojeek.html"), max_results=5)
    assert [r.url for r in results] == ["https://example.com/mojeek-1", "https://example.org/mojeek-2"]
    assert results[0].title == "Mojeek Result One"
    assert results[0].snippet == "Mojeek snippet one"
    assert results[0].engine == "mojeek"


def test_ecosia_parse():
    results = EcosiaEngine()._parse(_load_v2("ecosia.html"), max_results=5)
    assert [r.url for r in results] == ["https://example.com/ecosia-1", "https://example.org/ecosia-2"]
    assert results[0].title == "Ecosia Result One"
    assert results[0].snippet == "Ecosia snippet one"
    assert results[0].engine == "ecosia"


def test_startpage_parse_react_payload():
    results = StartpageEngine()._parse(_load_v2("startpage.html"), max_results=5)
    assert [r.url for r in results] == ["https://example.com/startpage-1", "https://example.org/startpage-2"]
    assert results[0].title == "Startpage Result One"  # inline <b> stripped
    assert results[0].snippet == "Startpage snippet one"
    assert results[0].engine == "startpage"


def test_startpage_html_fallback_without_payload():
    html = '<html><body><div class="result"><h3><a href="https://example.com/sp-fb">Fallback Title</a></h3><p>Fallback snippet</p></div></body></html>'
    results = StartpageEngine()._parse(html, max_results=5)
    assert len(results) == 1 and results[0].url == "https://example.com/sp-fb"


@pytest.mark.asyncio
async def test_startpage_captcha_redirect_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sp/captcha":
            return httpx.Response(200, text="<html>captcha</html>")
        return httpx.Response(302, headers={"location": "https://www.startpage.com/sp/captcha"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(EngineUnavailable, match="captcha"):
        await StartpageEngine(transport=transport).search("query", 5)
