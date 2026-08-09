import pytest

from proseforge.infrastructure.security.endpoint_policy import EndpointPolicy


def test_endpoint_policy_rejects_private_and_userinfo_urls():
    policy = EndpointPolicy(("ollama",))
    with pytest.raises(ValueError):
        policy.validate("http://127.0.0.1:11434")
    with pytest.raises(ValueError):
        policy.validate("https://user:pass@example.com")
    assert policy.validate("http://ollama:11434", allow_local=True).startswith("http://ollama")
