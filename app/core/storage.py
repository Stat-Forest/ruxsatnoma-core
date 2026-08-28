"""S3/MinIO adapter (ruling 2, 3.3b): the only place that talks to object storage.

aioboto3 behind three module functions; a client per call is fine at our load and
keeps the module free of global connection state (mirrors how sessions are made
per request). Swapping the library or adding presigned URLs later touches only
this file.
"""

from typing import TYPE_CHECKING

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.config import get_settings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

_session = aioboto3.Session()


def _client() -> S3Client:
    s = get_settings()
    return _session.client(  # pyright: ignore[reportReturnType]
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        config=BotoConfig(s3={"addressing_style": "path"}),  # MinIO needs path-style
    )


async def ensure_bucket() -> None:
    """Create the bucket if missing (fresh MinIO volume); idempotent."""
    s = get_settings()
    async with _client() as c:
        try:
            await c.head_bucket(Bucket=s.s3_bucket)
        except ClientError:
            await c.create_bucket(Bucket=s.s3_bucket)


async def put_object(key: str, data: bytes, content_type: str) -> None:
    s = get_settings()
    async with _client() as c:
        await c.put_object(Bucket=s.s3_bucket, Key=key, Body=data, ContentType=content_type)


async def get_object(key: str) -> bytes:
    s = get_settings()
    async with _client() as c:
        try:
            resp = await c.get_object(Bucket=s.s3_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from exc
            raise
        async with resp["Body"] as body:
            return await body.read()
