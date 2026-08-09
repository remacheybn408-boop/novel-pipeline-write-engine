import pytest

from proseforge.application.files.upload_service import (
    validate_upload,
    verify_download_digest,
)


def test_download_digest_rejects_tampered_blob():
    assert verify_download_digest(b"hello", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824") is True
    assert verify_download_digest(b"tampered", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824") is False


def test_upload_rejects_traversal():
    with pytest.raises(ValueError):
        validate_upload("../secret.txt", b"x", "text/plain")
