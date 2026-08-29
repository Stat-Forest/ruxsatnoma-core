"""In-memory per-IP token bucket (plan 03.4 ruling 12).

Per-process state, accepted deliberately: N uvicorn workers multiply the
effective limit by N. Real distributed limiting needs shared storage (Redis),
which decision #36 rejected for now; revisit if the process count grows."""

import math
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.deps import get_db
from app.core.errors import err


@dataclass
class _Bucket:
    tokens: float
    updated: float


_buckets: dict[tuple[str, str], _Bucket] = {}


def reset() -> None:
    """Tests only: forget every bucket."""
    _buckets.clear()


def rate_limit(scope: str, setting_key: str):
    """Dependency factory: at most `setting_key` requests per minute per client IP."""

    async def dep(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> None:
        per_minute = await settings_store.get_int(db, setting_key)
        per_minute = max(1, per_minute)  # never divide by zero, whatever the setting holds
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = _buckets.get((scope, ip))
        if bucket is None:
            bucket = _Bucket(tokens=float(per_minute), updated=now)
            _buckets[(scope, ip)] = bucket
        else:
            bucket.tokens = min(
                float(per_minute), bucket.tokens + (now - bucket.updated) * per_minute / 60.0
            )
            bucket.updated = now
        if bucket.tokens < 1.0:
            retry = math.ceil((1.0 - bucket.tokens) * 60.0 / per_minute)
            raise err("ERR-SYS-006", details={"retry_after_seconds": retry})
        bucket.tokens -= 1.0

    return dep
