"""SearchService: engine failover, in-process TTL cache, and safe page fetching.

``web_search`` tries the configured engines in order and returns the first
non-empty result set; ``web_fetch`` downloads one page through the SSRF
endpoint policy (every redirect hop re-validated) and reduces it to plain
text. Fetch failures are returned as ``(None, reason)`` — the caller decides
how to present them, nothing here raises for fetch problems.
"""

from __future__ import annotations

import re
import time

import httpx

from proseforge.infrastructure.search.engines import (
    ENGINE_TYPES,
    EngineUnavailable,
    SearchResult,
)
from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy
from proseforge.infrastructure.webtools.fetcher import SafeFetcher

CACHE_TTL_SECONDS = 600.0
# Boilerplate tags removed before text extraction.
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside")


def extract_text(html: str, max_chars: int) -> str:
    """Strip boilerplate tags, collapse whitespace, truncate to max_chars."""
    from bs4 import BeautifulSoup  # lazy: api-extra dependency

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:max_chars]


class SearchService:
    def __init__(
        self,
        *,
        engines: tuple[str, ...] | list[str] = ("bing", "duckduckgo", "baidu"),
        timeout_seconds: float = 10.0,
        endpoint_policy: EndpointPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._timeout_seconds = timeout_seconds
        self._policy = endpoint_policy or EndpointPolicy()
        # Unknown engine names in config are skipped, not fatal.
        self._engines = [ENGINE_TYPES[name](timeout_seconds, transport) for name in engines if name in ENGINE_TYPES]
        self._transport = transport
        self._fetcher = SafeFetcher(timeout_seconds=timeout_seconds, endpoint_policy=self._policy, transport=transport)
        self._cache: dict[tuple[str, int], tuple[float, list[SearchResult]]] = {}

    async def web_search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Try engines in config order; first success wins. Raises EngineUnavailable when all fail."""
        key = (query.strip().lower(), max_results)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        last_error: EngineUnavailable | None = None
        for engine in self._engines:
            try:
                results = await engine.search(query, max_results)
            except EngineUnavailable as exc:
                last_error = exc
                continue
            deduped = self._dedupe(results)
            self._cache[key] = (time.monotonic(), deduped)
            return deduped
        raise last_error or EngineUnavailable("no search engines configured")

    async def web_fetch(self, url: str, max_chars: int = 6000) -> tuple[str | None, str | None]:
        """Fetch one page as plain text. Returns (text, None) or (None, reason).

        The SSRF-safe fetch itself lives in webtools.SafeFetcher (endpoint
        policy on every redirect hop); this method only reduces HTML to text.
        """
        outcome = await self._fetcher.fetch_html(url)
        if outcome.error is not None or outcome.html is None:
            return None, outcome.error or "empty response"
        text = extract_text(outcome.html, max_chars)
        if not text:
            return None, "page contained no readable text"
        return text, None

    @staticmethod
    def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
        seen: set[str] = set()
        deduped: list[SearchResult] = []
        for result in results:
            if result.url in seen:
                continue
            seen.add(result.url)
            deduped.append(result)
        return deduped
