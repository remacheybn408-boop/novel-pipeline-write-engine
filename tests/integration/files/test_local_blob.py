import pytest

from proseforge.infrastructure.blob.local import LocalBlobStore


@pytest.mark.asyncio
async def test_same_bytes_use_same_content_address(tmp_path):
    store = LocalBlobStore(tmp_path)
    first = await store.put(data=b"same", media_type="text/plain")
    second = await store.put(data=b"same", media_type="text/plain")
    assert first == second
    assert await store.get(first) == b"same"
