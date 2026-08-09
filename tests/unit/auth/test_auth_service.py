import pytest

from proseforge.application.auth.service import AuthService, AuthUser


def test_password_hash_and_jwt_round_trip():
    service = AuthService("secret" * 8)
    password_hash = service.hash_password("a-long-enough-password")
    assert service.verify_password("a-long-enough-password", password_hash)
    assert service.decode_token(service.issue_token(AuthUser("u1", "a@example.com"))).id == "u1"


def test_short_password_rejected():
    with pytest.raises(ValueError):
        AuthService("secret" * 8).hash_password("short")
