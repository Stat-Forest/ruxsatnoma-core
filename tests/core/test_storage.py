"""Storage adapter against the real MinIO from docker-compose."""

import uuid

import pytest

from app.core import storage


async def test_put_then_get_roundtrip():
    await storage.ensure_bucket()
    key = f"test/{uuid.uuid4().hex}"
    await storage.put_object(key, b"hello minio", "text/plain")
    assert await storage.get_object(key) == b"hello minio"


async def test_get_missing_key_raises():
    await storage.ensure_bucket()
    with pytest.raises(FileNotFoundError):
        await storage.get_object(f"test/{uuid.uuid4().hex}")


async def test_ensure_bucket_is_idempotent():
    await storage.ensure_bucket()
    await storage.ensure_bucket()  # second call must not raise
