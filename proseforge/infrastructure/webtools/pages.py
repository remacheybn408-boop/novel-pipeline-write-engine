"""The three web tools: read_page / get_page_metadata / extract_links.

Honest status reporting — callers get a machine-readable ``status`` instead
of a fake success: ok / paywalled / login_required / js_required /
extraction_failed / timeout / ssrf_blocked. trafilatura and bs4 are lazy
imports (api extras); the classification helpers stay pure-python so they
are testable without those packages.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from proseforge.infrastructure.webtools.fetcher import FetchOutcome, SafeFetcher

STATUS_OK = "ok"
STATUS_PAYWALLED = "paywalled"
STATUS_LOGIN_REQUIRED = "login_required"
STATUS_JS_REQUIRED = "js_required"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_TIMEOUT = "timeout"
STATUS_SSRF_BLOCKED = "ssrf_blocked"

PAYWALL_WORDS = ("paywall", "subscribe", "subscription", "subscribing", "订阅", "付费", "会员专享", "开通会员")
SPA_SHELL_MARKERS = ('id="root"', 'id="app"', 'id="__next"', 'data-reactroot', "window.__NUXT__")
PAYWALL_MIN_TEXT_CHARS = 200

_JSONLD_FREE_FALSE = re.compile(r'"isAccessibleForFree"\s*:\s*(false|"false")', re.IGNORECASE)


def detect_paywall_jsonld(html: str) -> bool:
    """JSON-LD ``isAccessibleForFree: false`` — pure regex, no parser needed."""
    return bool(_JSONLD_FREE_FALSE.search(html))


def detect_spa_shell(html: str) -> bool:
    """True when the page is an empty SPA shell (nothing readable without JS)."""
    if not any(marker in html for marker in SPA_SHELL_MARKERS):
        return False
    # A shell has almost no visible text: strip tags crudely and measure.
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) < PAYWALL_MIN_TEXT_CHARS


def classify_fetch_error(outcome: FetchOutcome) -> str:
    if outcome.error_kind == "ssrf":
        return STATUS_SSRF_BLOCKED
    if outcome.error_kind == "timeout":
        return STATUS_TIMEOUT
    # An HTTP error status (e.g. anti-bot 403) is not an extraction problem.
    http_status = classify_http_status(outcome.status_code)
    if http_status is not None:
        return http_status
    return STATUS_EXTRACTION_FAILED


def classify_http_status(status_code: int) -> str | None:
    if status_code in (401, 403):
        return STATUS_LOGIN_REQUIRED
    return None


def looks_paywalled(text: str) -> bool:
    """Short body + paywall vocabulary — the page gave us a teaser, not content."""
    if len(text) >= PAYWALL_MIN_TEXT_CHARS:
        return False
    lowered = text.lower()
    return any(word in lowered for word in PAYWALL_WORDS)


def _extract_main_text(html: str) -> str | None:
    import trafilatura  # lazy: api-extra dependency

    return trafilatura.extract(html, include_tables=True, include_images=True, include_comments=False)


def _soup(html: str):
    from bs4 import BeautifulSoup  # lazy: api-extra dependency

    return BeautifulSoup(html, "html.parser")


def _page_metadata(html: str, final_url: str) -> dict[str, str]:
    soup = _soup(html)

    def meta(**attrs: str) -> str:
        node = soup.find("meta", attrs=attrs)
        return str(node.get("content", "")).strip() if node is not None else ""

    title = meta(property="og:title")
    if not title and soup.title is not None and soup.title.string:
        title = soup.title.string.strip()
    date = meta(property="article:published_time") or meta(name="date") or _jsonld_field(html, "datePublished")
    canonical_node = soup.find("link", rel="canonical")
    canonical = str(canonical_node.get("href", "")).strip() if canonical_node is not None else ""
    html_node = soup.find("html")
    language = str(html_node.get("lang", "")).strip() if html_node is not None else ""
    return {
        "title": title,
        "description": meta(property="og:description") or meta(name="description"),
        "author": meta(name="author"),
        "date": date,
        "site": meta(property="og:site_name"),
        "canonical_url": canonical or final_url,
        "language": language,
    }


def _jsonld_field(html: str, field: str) -> str:
    match = re.search(rf'"{field}"\s*:\s*"([^"]+)"', html)
    return match.group(1) if match else ""


def _confidence(text: str, soup) -> str:
    """Length + link-density heuristic: long prose with few links reads high."""
    if not text:
        return "low"
    link_chars = sum(len(anchor.get_text()) for anchor in soup.find_all("a"))
    density = link_chars / max(len(text), 1)
    if len(text) >= 1500 and density < 0.3:
        return "high"
    if len(text) >= 500:
        return "medium"
    return "low"


async def read_page(url: str, *, mode: str = "full", max_length: int = 4000, fetcher: SafeFetcher) -> dict[str, object]:
    outcome = await fetcher.fetch_html(url)
    if outcome.error is not None or outcome.html is None:
        return {"status": classify_fetch_error(outcome), "url": url, "error": outcome.error or "empty response"}
    early = classify_http_status(outcome.status_code)
    if early is not None:
        return {"status": early, "url": outcome.final_url}
    if detect_paywall_jsonld(outcome.html):
        return {"status": STATUS_PAYWALLED, "url": outcome.final_url}
    text = _extract_main_text(outcome.html)
    if text is None:
        status = STATUS_JS_REQUIRED if detect_spa_shell(outcome.html) else STATUS_EXTRACTION_FAILED
        return {"status": status, "url": outcome.final_url}
    text = text.strip()
    if looks_paywalled(text):
        return {"status": STATUS_PAYWALLED, "url": outcome.final_url}
    metadata = _page_metadata(outcome.html, outcome.final_url)
    limit = max(200, max_length // 3) if mode == "summary" else max(200, max_length)
    return {
        "status": STATUS_OK,
        "title": metadata["title"],
        "text": text[:limit],
        "author": metadata["author"],
        "date": metadata["date"],
        "site": metadata["site"],
        "canonical_url": metadata["canonical_url"],
        "language": metadata["language"],
        "confidence": _confidence(text, _soup(outcome.html)),
    }


async def get_page_metadata(url: str, *, fetcher: SafeFetcher) -> dict[str, object]:
    outcome = await fetcher.fetch_html(url)
    if outcome.error is not None or outcome.html is None:
        return {"status": classify_fetch_error(outcome), "url": url, "error": outcome.error or "empty response"}
    early = classify_http_status(outcome.status_code)
    if early is not None:
        return {"status": early, "url": outcome.final_url}
    metadata = _page_metadata(outcome.html, outcome.final_url)
    return {
        "status": STATUS_OK,
        "title": metadata["title"],
        "description": metadata["description"],
        "date": metadata["date"],
        "site": metadata["site"],
        "canonical_url": metadata["canonical_url"],
    }


async def extract_links(url: str, *, max_links: int = 20, fetcher: SafeFetcher) -> dict[str, object]:
    outcome = await fetcher.fetch_html(url)
    if outcome.error is not None or outcome.html is None:
        return {"status": classify_fetch_error(outcome), "url": url, "error": outcome.error or "empty response", "links": []}
    early = classify_http_status(outcome.status_code)
    if early is not None:
        return {"status": early, "url": outcome.final_url, "links": []}
    soup = _soup(outcome.html)
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(outcome.final_url, href)
        if not absolute.startswith("http") or absolute in seen:
            continue
        seen.add(absolute)
        links.append({"text": re.sub(r"\s+", " ", anchor.get_text(" ")).strip() or absolute, "url": absolute})
        if len(links) >= max_links:
            break
    return {"status": STATUS_OK, "url": outcome.final_url, "links": links, "count": len(links)}
