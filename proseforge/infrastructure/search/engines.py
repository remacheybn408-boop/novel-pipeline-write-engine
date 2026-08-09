"""HTML-scraping search engine adapters for the built-in web search tool.

Each adapter wraps one public no-JS search endpoint and parses the result
page with BeautifulSoup CSS selectors. Contract: return real results or raise
``EngineUnavailable`` — never return an empty list to fake success, because an
empty list looks like "no hits" and would stop the service's failover chain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from proseforge.infrastructure.webtools.fetcher import BROWSER_UA


class EngineUnavailable(Exception):
    """One engine endpoint is down, blocked, or returned an unparseable page."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _HtmlEngine:
    """Shared HTTP + error-mapping plumbing for the three scrapers."""

    name = ""
    extra_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, timeout_seconds: float = 10.0, transport: httpx.BaseTransport | None = None):
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", **self.extra_headers}

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport, follow_redirects=True) as client:
                response = await self._request(client, query, max_results)
                response.raise_for_status()
                self._check_response(response)
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"{self.name}: HTTP error: {exc}") from exc
        results = self._parse(response.text, max_results)
        if not results:
            raise EngineUnavailable(f"{self.name}: page parsed but no result nodes matched (markup may have changed)")
        return results

    def _check_response(self, response: httpx.Response) -> None:
        """Post-status hook for captcha/block-page detection (default: no-op)."""

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        raise NotImplementedError

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        raise NotImplementedError


class DuckDuckGoEngine(_HtmlEngine):
    name = "duckduckgo"

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        # html.duckduckgo.com is the documented no-JS endpoint; POST is the
        # classic form submission and the most reliable variant.
        return await client.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import (
            BeautifulSoup,  # lazy: api-extra dependency, keep module importable without it
        )

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select("div.result"):
            link = node.select_one("a.result__a")
            if link is None:
                continue
            url = self._unwrap(str(link.get("href", "")))
            if not url:
                continue
            snippet_node = node.select_one(".result__snippet")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(link.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results

    @staticmethod
    def _unwrap(href: str) -> str:
        # DDG wraps outbound links as //duckduckgo.com/l/?uddg=<urlencoded>.
        if not href:
            return ""
        if href.startswith("//"):
            href = f"https:{href}"
        parsed = urlparse(href)
        if "duckduckgo.com" in (parsed.hostname or ""):
            target = parse_qs(parsed.query).get("uddg")
            if target and target[0]:
                return target[0]
        return href


class BingEngine(_HtmlEngine):
    name = "bing"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.bing.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://www.bing.com/search", params={"q": query, "count": max_results}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select("li.b_algo"):
            header = node.select_one("h2 a")
            if header is None:
                continue
            url = str(header.get("href", ""))
            if not url.startswith("http"):
                continue
            snippet_node = node.select_one(".b_caption p")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(header.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


class BaiduEngine(_HtmlEngine):
    name = "baidu"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.baidu.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://www.baidu.com/s", params={"wd": query, "rn": max_results}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select("div.result"):
            header = node.select_one("h3 a")
            if header is None:
                continue
            # Baidu result URLs are baidu.com/link?url= redirect hops — kept as-is.
            url = str(header.get("href", ""))
            if not url.startswith("http"):
                continue
            snippet_node = node.select_one(".c-abstract")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(header.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


def _strip_html(text: str) -> str:
    """Reduce an HTML fragment (e.g. an embedded-JSON field) to plain text."""
    from bs4 import BeautifulSoup  # lazy: api-extra dependency

    return _collapse(BeautifulSoup(text, "html.parser").get_text(" "))


class GoogleEngine(_HtmlEngine):
    """Selectors follow SearXNG's google engine: result title anchors carry a
    ``data-ved`` attribute and no class (obfuscated class names like
    ``egMi0 kCrYT`` are unreliable); the snippet lives in a ``div.ilUpNd``
    under the anchor's grandparent."""

    name = "google"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.google.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        # CONSENT cookie skips the EU consent wall (same trick as searxng).
        return await client.get(
            "https://www.google.com/search",
            params={"q": query, "hl": "en", "ie": "utf8", "oe": "utf8", "filter": "0", "num": max_results},
            headers=self.headers,
            cookies={"CONSENT": "YES+"},
        )

    def _check_response(self, response: httpx.Response) -> None:
        if response.url.host == "sorry.google.com" or response.url.path.startswith("/sorry"):
            raise EngineUnavailable("google: captcha (redirected to sorry page)")
        if "unusual traffic" in response.text.lower():
            raise EngineUnavailable("google: captcha (unusual traffic page)")

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for anchor in soup.find_all("a", attrs={"data-ved": True}):
            if anchor.get("class"):
                continue
            title_node = anchor.find("div", attrs={"style": True})
            if title_node is None:
                continue
            url = self._unwrap(str(anchor.get("href", "")))
            if not url.startswith("http"):
                continue
            snippet = ""
            container = anchor.parent.parent if anchor.parent is not None and anchor.parent.parent is not None else None
            if container is not None:
                snippet_node = container.select_one("div.ilUpNd")
                if snippet_node is not None:
                    snippet = _collapse(snippet_node.get_text(" "))
            results.append(SearchResult(_collapse(title_node.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results

    @staticmethod
    def _unwrap(href: str) -> str:
        # Strip the google /url?q=<url>&sa=U redirector.
        if href.startswith("/url?q="):
            return unquote(href[len("/url?q="):].split("&sa=U")[0])
        return href


class YahooEngine(_HtmlEngine):
    """Selectors follow SearXNG's yahoo engine: containers are
    ``div[class*=algo-sr]``, title/link in ``div.compTitle h3 a`` (title from
    the anchor's aria-label), snippet in ``div.compText``. Tracking hops via
    r.search.yahoo.com are unwrapped (real URL between /RU= and /RS|/RK)."""

    name = "yahoo"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://search.yahoo.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        # iscqry (empty) is needed for first-page results to render properly.
        return await client.get("https://search.yahoo.com/search", params={"p": query, "iscqry": ""}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select('div[class*="algo-sr"]'):
            link = node.select_one('div[class*="compTitle"] h3 a') or node.select_one('div[class*="compTitle"] > a')
            if link is None:
                continue
            url = self._unwrap(str(link.get("href", "")))
            if not url.startswith("http"):
                continue
            title = str(link.get("aria-label") or "") or _collapse(link.get_text(" "))
            snippet_node = node.select_one('div[class*="compText"]')
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(title, url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results

    @staticmethod
    def _unwrap(url: str) -> str:
        # r.search.yahoo.com hop: real URL sits urlencoded between /RU= and /RS|/RK.
        if "/RU=" not in url:
            return url
        start = url.find("http", url.find("/RU=") + 1)
        ends = [pos for pos in (url.rfind("/RS"), url.rfind("/RK")) if pos > -1]
        if start <= 0 or not ends:
            return url
        return unquote(url[start:min(ends)])


class BraveEngine(_HtmlEngine):
    """Selectors follow SearXNG's brave engine: containers are ``div.snippet``,
    url is the first absolute ``a`` href (relative href = ad, skipped), title
    in ``div[class*=title]``, snippet in the div with the ``content`` class
    token (token-exact to avoid matching ``site-name-content``)."""

    name = "brave"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://search.brave.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://search.brave.com/search", params={"q": query, "source": "web"}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.find_all("div", class_="snippet"):
            anchor = node.find("a", href=True)
            title_node = node.select_one('div[class*="title"]')
            if anchor is None or title_node is None:
                continue
            url = str(anchor["href"])
            if not urlparse(url).netloc:
                continue  # partial url likely means it's an ad
            content_node = node.find("div", class_="content")
            snippet = _collapse(content_node.get_text(" ")) if content_node else ""
            results.append(SearchResult(_collapse(title_node.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


class MojeekEngine(_HtmlEngine):
    """Selectors follow SearXNG's mojeek engine: ``ul.results-standard li``
    containers, url from ``a.ob``, title from ``h2 a``, snippet from ``p.s``."""

    name = "mojeek"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.mojeek.com/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://www.mojeek.com/search", params={"q": query}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select("ul.results-standard li"):
            url_node = node.select_one("a.ob")
            title_node = node.select_one("h2 a")
            if url_node is None or title_node is None:
                continue
            url = str(url_node.get("href", ""))
            if not url.startswith("http"):
                continue
            snippet_node = node.find("p", class_="s")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(title_node.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


class EcosiaEngine(_HtmlEngine):
    """NOTE: SearXNG has no ecosia engine definition (removed upstream), so
    there is no authoritative selector source — containers are ``article``
    (falling back to ``div.result``) per the community-known SERP structure.
    Must be calibrated against real pages after deploy."""

    name = "ecosia"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.ecosia.org/"}

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://www.ecosia.org/search", params={"q": query}, headers=self.headers)

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select("article") or soup.select("div.result")
        results: list[SearchResult] = []
        for node in nodes:
            anchor = node.select_one("h2 a") or node.find("a", href=re.compile(r"^https?://"))
            if anchor is None:
                continue
            url = str(anchor.get("href", ""))
            if not url.startswith("http"):
                continue
            snippet_node = node.find("p")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(anchor.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


class StartpageEngine(_HtmlEngine):
    """Selectors follow SearXNG's startpage engine: results are parsed from the
    embedded React payload ``React.createElement(UIStartpage.AppSerpWeb, {...})``
    (render.presenter.regions.mainline[].results[] with display_type
    ``web-google``), NOT from result divs. A ``div.result`` HTML fallback is
    kept for pages without the payload. Captcha redirects raise."""

    name = "startpage"
    extra_headers: ClassVar[dict[str, str]] = {"Referer": "https://www.startpage.com/"}
    _REACT_MARKER = "React.createElement(UIStartpage.AppSerpWeb, {"

    async def _request(self, client: httpx.AsyncClient, query: str, max_results: int) -> httpx.Response:
        return await client.get("https://www.startpage.com/sp/search", params={"query": query, "cat": "web"}, headers=self.headers)

    def _check_response(self, response: httpx.Response) -> None:
        if response.url.path.startswith("/sp/captcha"):
            raise EngineUnavailable("startpage: captcha (redirected to /sp/captcha)")

    def _parse(self, html: str, max_results: int) -> list[SearchResult]:
        # The marker can also appear in comments/scripts that are not the real
        # payload — walk every occurrence until one yields usable results.
        position = 0
        while True:
            start = html.find(self._REACT_MARKER, position)
            if start < 0:
                break
            payload = self._extract_react_json(html, start + len(self._REACT_MARKER))
            if payload is not None:
                results = self._results_from_payload(payload, max_results)
                if results:
                    return results
            position = start + len(self._REACT_MARKER)
        return self._parse_html_fallback(html, max_results)

    @staticmethod
    def _extract_react_json(html: str, start: int) -> dict | None:
        # The payload ends at "}})"; nested objects may contain "}}" so each
        # candidate end position is tried until one parses as valid JSON.
        index = start
        while True:
            end = html.find("}})", index)
            if end < 0:
                return None
            try:
                parsed = json.loads("{" + html[start:end] + "}}")
            except ValueError:
                index = end + 3
                continue
            return parsed if isinstance(parsed, dict) else None

    def _results_from_payload(self, payload: dict, max_results: int) -> list[SearchResult]:
        regions = payload.get("render", {}).get("presenter", {}).get("regions", {})
        results: list[SearchResult] = []
        for group in regions.get("mainline", []):
            if group.get("display_type") != "web-google":
                continue
            for item in group.get("results", []):
                url = str(item.get("clickUrl", ""))
                if not url.startswith("http"):
                    continue
                title = _strip_html(str(item.get("title", "")))
                snippet = _strip_html(str(item.get("description", "")))
                results.append(SearchResult(title, url, snippet, self.name))
                if len(results) >= max_results:
                    return results
        return results

    def _parse_html_fallback(self, html: str, max_results: int) -> list[SearchResult]:
        from bs4 import BeautifulSoup  # lazy: api-extra dependency

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []
        for node in soup.select("div.result"):
            anchor = node.select_one("h3 a") or node.find("a", href=re.compile(r"^https?://"))
            if anchor is None:
                continue
            url = str(anchor.get("href", ""))
            if not url.startswith("http"):
                continue
            snippet_node = node.find("p")
            snippet = _collapse(snippet_node.get_text(" ")) if snippet_node else ""
            results.append(SearchResult(_collapse(anchor.get_text(" ")), url, snippet, self.name))
            if len(results) >= max_results:
                break
        return results


ENGINE_TYPES: dict[str, type[_HtmlEngine]] = {
    "duckduckgo": DuckDuckGoEngine,
    "bing": BingEngine,
    "baidu": BaiduEngine,
    "google": GoogleEngine,
    "yahoo": YahooEngine,
    "brave": BraveEngine,
    "mojeek": MojeekEngine,
    "ecosia": EcosiaEngine,
    "startpage": StartpageEngine,
}
