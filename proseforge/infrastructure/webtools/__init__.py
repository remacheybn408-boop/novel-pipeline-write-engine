"""Web tools: shared SSRF-safe fetcher + read_page / get_page_metadata / extract_links."""

from proseforge.infrastructure.webtools.fetcher import (
    BytesOutcome,
    FetchOutcome,
    SafeFetcher,
)

__all__ = ["BytesOutcome", "FetchOutcome", "SafeFetcher"]
