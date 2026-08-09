"""Shared SSRF-safe HTTP fetcher for web tools (and the search fetch path).

Single home for the security-critical fetch logic — endpoint-policy
validation on the initial URL AND every redirect hop, per-domain token
bucket, per-domain circuit breaker, in-process TTL cache, and bounded
exponential-backoff retries. Do NOT fork this logic into callers.

Failure taxonomy (FetchOutcome.error_kind): ``ssrf`` (policy blocked),
``timeout``, ``circuit`` (domain breaker open), ``upstream`` (HTTP/network).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy

# Canonical browser UA for all outbound scraping (engines import it from here
# to keep the dependency direction one-way: search -> webtools).
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

MAX_REDIRECT_HOPS = 5
RETRY_BACKOFF_SECONDS = (1.0, 2.0)  # two retries, only on timeout/429/5xx/network
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 300.0
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # hard cap for binary document downloads


@dataclass(frozen=True)
class FetchOutcome:
    html: str | None
    final_url: str
    status_code: int
    error: str | None = None
    error_kind: str | None = None
    cache_hit: bool = False


@dataclass(frozen=True)
class BytesOutcome:
    """Binary download result; ``too_large`` means the stream was aborted at
    the byte cap (no partial body is kept)."""

    data: bytes | None
    final_url: str
    status_code: int
    error: str | None = None
    error_kind: str | None = None
    too_large: bool = False


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        endpoint_policy: EndpointPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        cache_ttl_seconds: float = 600.0,
        rate_seconds_per_domain: float = 1.0,
    ):
        self._timeout = timeout_seconds
        self._policy = endpoint_policy or EndpointPolicy()
        self._transport = transport
        self._cache_ttl = cache_ttl_seconds
        self._rate_seconds = rate_seconds_per_domain
        self._cache: dict[str, tuple[float, FetchOutcome]] = {}
        self._domain_last_request: dict[str, float] = {}
        self._domain_failures: dict[str, int] = {}
        self._domain_open_until: dict[str, float] = {}

    async def fetch_html(self, url: str) -> FetchOutcome:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            outcome = cached[1]
            return FetchOutcome(outcome.html, outcome.final_url, outcome.status_code, outcome.error, outcome.error_kind, cache_hit=True)
        try:
            current = self._policy.validate(url)
        except ValueError as exc:
            return FetchOutcome(None, url, 0, f"url blocked: {exc}", "ssrf")
        domain = urlparse(current).hostname or ""
        open_until = self._domain_open_until.get(domain, 0.0)
        if time.monotonic() < open_until:
            return FetchOutcome(None, url, 0, f"circuit open for domain {domain}: too many recent failures", "circuit")
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), transport=self._transport, follow_redirects=False) as client:
            outcome = await self._fetch_with_retries(client, current)
        self._record(domain, outcome)
        if outcome.error is None:
            self._cache[url] = (time.monotonic(), outcome)
        return outcome

    async def _fetch_with_retries(self, client: httpx.AsyncClient, url: str) -> FetchOutcome:
        attempts = 1 + len(RETRY_BACKOFF_SECONDS)
        last: FetchOutcome | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            last = await self._fetch_once(client, url)
            if last.error_kind not in {"timeout", "upstream"}:
                return last
            if last.status_code and last.status_code < 500 and last.status_code != 429:
                return last  # 4xx (except 429) is a definitive answer, not transient
        return last  # type: ignore[return-value]

    async def _fetch_once(self, client: httpx.AsyncClient, url: str) -> FetchOutcome:
        current = url
        await self._throttle(urlparse(current).hostname or "")
        try:
            for _ in range(MAX_REDIRECT_HOPS):
                try:
                    response = await client.get(current, headers={"User-Agent": BROWSER_UA})
                except httpx.TimeoutException as exc:
                    return FetchOutcome(None, current, 0, f"fetch timeout: {exc}", "timeout")
                except httpx.HTTPError as exc:
                    return FetchOutcome(None, current, 0, f"fetch failed: {exc}", "upstream")
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return FetchOutcome(None, current, response.status_code, "redirect without Location header", "upstream")
                    try:
                        current = self._policy.validate(urljoin(current, location))
                    except ValueError as exc:
                        return FetchOutcome(None, current, response.status_code, f"redirect target blocked: {exc}", "ssrf")
                    continue
                if response.status_code >= 400:
                    kind = "upstream"
                    return FetchOutcome(None, current, response.status_code, f"HTTP {response.status_code}", kind)
                return FetchOutcome(response.text, current, response.status_code)
            return FetchOutcome(None, current, 0, f"too many redirects (>{MAX_REDIRECT_HOPS})", "upstream")
        finally:
            self._domain_last_request[urlparse(current).hostname or ""] = time.monotonic()

    async def fetch_bytes(self, url: str, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> BytesOutcome:
        """Stream a binary download with a hard byte cap.

        Same SSRF endpoint policy (initial URL + every redirect hop), domain
        throttle, circuit breaker and transient-retry rules as fetch_html —
        only the body handling differs (streamed, counted, aborted past the
        cap; no TTL cache for potentially large binaries).
        """
        try:
            current = self._policy.validate(url)
        except ValueError as exc:
            return BytesOutcome(None, url, 0, f"url blocked: {exc}", "ssrf")
        domain = urlparse(current).hostname or ""
        open_until = self._domain_open_until.get(domain, 0.0)
        if time.monotonic() < open_until:
            return BytesOutcome(None, url, 0, f"circuit open for domain {domain}: too many recent failures", "circuit")
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout), transport=self._transport, follow_redirects=False) as client:
            outcome = await self._fetch_bytes_with_retries(client, current, max_bytes)
        # Circuit-breaker bookkeeping shares the html path's rules.
        self._record(domain, FetchOutcome(None if outcome.data is None else "", outcome.final_url, outcome.status_code, outcome.error, outcome.error_kind))
        return outcome

    async def _fetch_bytes_with_retries(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> BytesOutcome:
        attempts = 1 + len(RETRY_BACKOFF_SECONDS)
        last: BytesOutcome | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            last = await self._fetch_bytes_once(client, url, max_bytes)
            if last.error_kind not in {"timeout", "upstream"}:
                return last  # ssrf/too_large/success are definitive
            if last.status_code and last.status_code < 500 and last.status_code != 429:
                return last  # 4xx (except 429) is a definitive answer
        return last  # type: ignore[return-value]

    async def _fetch_bytes_once(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> BytesOutcome:
        current = url
        await self._throttle(urlparse(current).hostname or "")
        try:
            for _ in range(MAX_REDIRECT_HOPS):
                try:
                    async with client.stream("GET", current, headers={"User-Agent": BROWSER_UA}) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                return BytesOutcome(None, current, response.status_code, "redirect without Location header", "upstream")
                            try:
                                current = self._policy.validate(urljoin(current, location))
                            except ValueError as exc:
                                return BytesOutcome(None, current, response.status_code, f"redirect target blocked: {exc}", "ssrf")
                            continue
                        if response.status_code >= 400:
                            return BytesOutcome(None, current, response.status_code, f"HTTP {response.status_code}", "upstream")
                        chunks: list[bytes] = []
                        received = 0
                        async for chunk in response.aiter_bytes(65536):
                            received += len(chunk)
                            if received > max_bytes:
                                return BytesOutcome(None, current, response.status_code, f"document exceeds {max_bytes} bytes", "too_large", too_large=True)
                            chunks.append(chunk)
                        return BytesOutcome(b"".join(chunks), current, response.status_code)
                except httpx.TimeoutException as exc:
                    return BytesOutcome(None, current, 0, f"fetch timeout: {exc}", "timeout")
                except httpx.HTTPError as exc:
                    return BytesOutcome(None, current, 0, f"fetch failed: {exc}", "upstream")
            return BytesOutcome(None, current, 0, f"too many redirects (>{MAX_REDIRECT_HOPS})", "upstream")
        finally:
            self._domain_last_request[urlparse(current).hostname or ""] = time.monotonic()

    async def _throttle(self, domain: str) -> None:
        if self._rate_seconds <= 0:
            return
        last = self._domain_last_request.get(domain, 0.0)
        wait = self._rate_seconds - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def _record(self, domain: str, outcome: FetchOutcome) -> None:
        # Only transient infrastructure failures count toward the breaker;
        # 4xx answers (except 429) and SSRF blocks are definitive.
        transient = outcome.error_kind in {"timeout", "upstream"} and (not outcome.status_code or outcome.status_code >= 500 or outcome.status_code == 429)
        if outcome.error is None or not transient:
            if outcome.error is None:
                self._domain_failures[domain] = 0
            return
        failures = self._domain_failures.get(domain, 0) + 1
        self._domain_failures[domain] = failures
        if failures >= BREAKER_THRESHOLD:
            self._domain_open_until[domain] = time.monotonic() + BREAKER_COOLDOWN_SECONDS
            self._domain_failures[domain] = 0
