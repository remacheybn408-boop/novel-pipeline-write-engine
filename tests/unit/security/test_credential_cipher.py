import os

import pytest
from cryptography.exceptions import InvalidTag

from proseforge.infrastructure.security.credential_cipher import CredentialCipher


def test_cipher_round_trip_and_tamper_rejection():
    cipher = CredentialCipher(os.urandom(32))
    encrypted = bytearray(cipher.encrypt(b"secret", associated_data=b"user/provider/id"))
    assert cipher.decrypt(bytes(encrypted), associated_data=b"user/provider/id") == b"secret"
    encrypted[-1] ^= 1
    with pytest.raises(InvalidTag):
        cipher.decrypt(bytes(encrypted), associated_data=b"user/provider/id")
