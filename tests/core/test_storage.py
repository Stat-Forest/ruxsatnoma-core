"""Storage adapter against the real MinIO from docker-compose."""

import asyncio
import uuid

import pytest

from app.config import get_settings
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


async def test_ensure_bucket_survives_four_of_itself_at_once(monkeypatch: pytest.MonkeyPatch):
    """The idempotency test above calls it twice IN SEQUENCE, so the second call
    finds the bucket and `create_bucket` is never reached — it proves nothing
    about two callers arriving together, which is what `-n 4` made routine
    (decision #92).

    On a fresh MinIO every worker's `head_bucket` answers 404 within the same
    instant and every worker then calls `create_bucket`; one wins and the rest
    get `BucketAlreadyOwnedByYou`, which was not caught. It surfaced as
    `ERROR at setup` on an unrelated test and a red CI on whatever branch
    happened to be running.

    A unique bucket name reproduces the fresh-volume path for real, without
    deleting the shared one out from under a parallel session.
    """
    fresh = f"race-{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv("S3_BUCKET", fresh)
    get_settings.cache_clear()
    try:
        await asyncio.gather(*(storage.ensure_bucket() for _ in range(4)))
        # And it really was created, rather than every caller swallowing a failure.
        key = f"test/{uuid.uuid4().hex}"
        await storage.put_object(key, b"raced", "text/plain")
        assert await storage.get_object(key) == b"raced"
    finally:
        get_settings.cache_clear()
