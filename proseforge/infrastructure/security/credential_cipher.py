from __future__ import annotations

import base64
import binascii
import hashlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_key(raw: str) -> bytes:
    """Derive the 32-byte cipher key from the master key setting.

    Single derivation rule used by every encrypt/decrypt site: base64-decode,
    and if that fails or does not yield exactly 32 bytes, fall back to
    SHA-256 of the raw string.
    """
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, binascii.Error):
        key = b""
    if len(key) != 32:
        key = hashlib.sha256(raw.encode()).digest()
    return key


class CredentialCipher:
    VERSION = 1

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("credential cipher key must be 32 bytes")
        self._key = key

    def encrypt(self, plaintext: bytes, *, associated_data: bytes = b"") -> bytes:
        import os
        nonce = os.urandom(12)
        return bytes([self.VERSION]) + nonce + AESGCM(self._key).encrypt(nonce, plaintext, associated_data)

    def decrypt(self, payload: bytes, *, associated_data: bytes = b"") -> bytes:
        if len(payload) < 1 + 12 + 16 or payload[0] != self.VERSION:
            raise InvalidTag
        return AESGCM(self._key).decrypt(payload[1:13], payload[13:], associated_data)
