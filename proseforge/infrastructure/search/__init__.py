"""Built-in web search: HTML engine scrapers + failover service."""

from proseforge.infrastructure.search.engines import EngineUnavailable, SearchResult
from proseforge.infrastructure.search.service import SearchService

__all__ = ["EngineUnavailable", "SearchResult", "SearchService"]
