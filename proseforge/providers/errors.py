from __future__ import annotations

import httpx

from proseforge.domain.common.errors import ProviderError, RetryableProviderError


class ProviderAuthenticationError(ProviderError):
    code = "PROVIDER_AUTH"


class ProviderRateLimitError(RetryableProviderError):
    code = "PROVIDER_RATE_LIMIT"


class ProviderTimeoutError(RetryableProviderError):
    code = "PROVIDER_TIMEOUT"


class ProviderUnavailableError(RetryableProviderError):
    code = "PROVIDER_UNAVAILABLE"


class ProviderUpstreamError(ProviderError):
    code = "PROVIDER_UPSTREAM"


def classify_provider_error(error: Exception) -> Exception:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, httpx.TimeoutException):
        return ProviderTimeoutError("provider request timed out")
    if isinstance(error, httpx.ConnectError | httpx.NetworkError):
        return ProviderUnavailableError("provider connection failed")
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code if error.response is not None else 0
        if status in {401, 403}:
            return ProviderAuthenticationError("provider credentials were rejected")
        if status == 429:
            return ProviderRateLimitError("provider rate limit reached")
        if status >= 500:
            return ProviderUnavailableError("provider returned a server error")
        return ProviderUpstreamError(f"provider request failed with HTTP {status}")
    return error
