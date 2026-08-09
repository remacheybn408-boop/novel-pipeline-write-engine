"""Offline tests for the webtools page classification helpers (pure python —
no bs4/trafilatura needed). trafilatura-dependent paths are marked for the
server run via importorskip.
"""

from __future__ import annotations

import pytest

from proseforge.infrastructure.webtools.pages import (
    STATUS_JS_REQUIRED,
    STATUS_LOGIN_REQUIRED,
    classify_http_status,
    detect_paywall_jsonld,
    detect_spa_shell,
    looks_paywalled,
)


def test_paywall_jsonld_detected():
    html = '<html><head><script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree":false}</script></head><body>x</body></html>'
    assert detect_paywall_jsonld(html)
    assert not detect_paywall_jsonld('<script type="application/ld+json">{"isAccessibleForFree":true}</script>')


def test_spa_shell_detected():
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    assert detect_spa_shell(shell)
    assert not detect_spa_shell('<html><body><div id="root">' + "正文" * 200 + "</div></body></html>")
    assert not detect_spa_shell("<html><body><article>" + "正文" * 200 + "</article></body></html>")


def test_login_required_status_codes():
    assert classify_http_status(401) == STATUS_LOGIN_REQUIRED
    assert classify_http_status(403) == STATUS_LOGIN_REQUIRED
    assert classify_http_status(404) is None
    assert classify_http_status(200) is None


def test_looks_paywalled_short_teaser():
    assert looks_paywalled("Subscribe to read the full article.") is True
    assert looks_paywalled("订阅会员后可阅读全文") is True
    assert looks_paywalled("普通正文。") is False
    assert looks_paywalled("subscribe " + "长" * 500) is False  # long text is not a teaser


def test_read_page_spa_shell_status():
    pytest.importorskip("trafilatura")  # server-side only
    import asyncio

    import httpx

    from proseforge.infrastructure.webtools import SafeFetcher
    from proseforge.infrastructure.webtools.pages import read_page

    shell = '<html><body><div id="__next"></div><script src="/app.js"></script></body></html>'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=shell))
    fetcher = SafeFetcher(transport=transport, rate_seconds_per_domain=0)
    outcome = asyncio.run(read_page("https://spa.example/", fetcher=fetcher))
    assert outcome["status"] == STATUS_JS_REQUIRED
