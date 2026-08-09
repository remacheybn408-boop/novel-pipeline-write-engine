import httpx

from proseforge.domain.common.errors import ProviderError, RetryableProviderError
from proseforge.providers.errors import classify_provider_error


def test_provider_errors_are_classified_for_retry_and_user_feedback():
    timeout = classify_provider_error(httpx.ReadTimeout("slow"))
    assert isinstance(timeout, RetryableProviderError)
    assert timeout.code == "PROVIDER_TIMEOUT"

    response = httpx.Response(401, request=httpx.Request("GET", "https://provider.test"))
    auth = classify_provider_error(httpx.HTTPStatusError("bad key", request=response.request, response=response))
    assert isinstance(auth, ProviderError)
    assert auth.code == "PROVIDER_AUTH"
    assert auth.retryable is False
