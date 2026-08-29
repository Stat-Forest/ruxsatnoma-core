"""Idempotency-Key on critical POSTs (design/03; plan 03.4 ruling 13).

Core holds the mechanism; the FastAPI dependency that resolves the current
user lives in auth.deps (core imports no modules). Flow: `begin()` inserts and
COMMITS a marker row so concurrent duplicates see it immediately; a stored
response replays via StoredIdempotentResponse (handled in main.py); the route
handler calls ctx.save() before returning; markers older than 5 minutes with
no response are re-claimed (a crashed handler must not block retries);
completed rows are purged by the nightly job."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import IdempotencyKey

IN_FLIGHT_TTL = timedelta(minutes=5)


class StoredIdempotentResponse(Exception):
    """Raised to short-circuit a replayed request; main.py turns it into a response."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        super().__init__(status_code)
        self.status_code = status_code
        self.body = body


@dataclass
class IdempotencyContext:
    key: uuid.UUID
    user_id: uuid.UUID
    fresh: bool

    async def save(self, db: AsyncSession, *, status_code: int, body: dict[str, Any]) -> None:
        """Persist the handler's response for replays; caller's transaction commits it."""
        await db.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key == self.key, IdempotencyKey.user_id == self.user_id)
            .values(response_status=status_code, response_body=body)
        )


def fingerprint(method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(f"{method}|{path}|".encode())
    digest.update(body)
    return digest.hexdigest()


async def begin(
    db: AsyncSession,
    *,
    key: uuid.UUID,
    user_id: uuid.UUID,
    method: str,
    path: str,
    body: bytes,
) -> IdempotencyContext:
    fp = fingerprint(method, path, body)
    inserted = (
        await db.execute(
            pg_insert(IdempotencyKey)
            .values(key=key, user_id=user_id, fingerprint=fp, route=f"{method} {path}")
            .on_conflict_do_nothing(index_elements=["key", "user_id"])
            .returning(IdempotencyKey.key)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        await db.commit()  # marker must be visible to concurrent duplicates NOW
        return IdempotencyContext(key=key, user_id=user_id, fresh=True)

    # populate_existing: this session may already hold a cached copy of this row from
    # an earlier begin() call on the same key (identity map) — with expire_on_commit=False
    # a plain get() would return that stale copy instead of the row's current DB state.
    row = await db.get(IdempotencyKey, (key, user_id), populate_existing=True)
    if row is None:
        # We conflicted on insert, but the row is gone by the time we looked it up —
        # a concurrent request's stale re-claim (below) deleted it in between. Retry:
        # our own insert is very likely to succeed now.
        return await begin(db, key=key, user_id=user_id, method=method, path=path, body=body)
    if row.fingerprint != fp:
        raise err("ERR-SYS-005", details={"reason": "fingerprint_mismatch"})
    if row.response_status is not None and row.response_body is not None:
        raise StoredIdempotentResponse(row.response_status, row.response_body)
    if row.created_at < datetime.now(UTC) - IN_FLIGHT_TTL:
        # Stale in-flight marker (crashed handler): re-claim it.
        await db.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.key == key, IdempotencyKey.user_id == user_id
            )
        )
        await db.commit()
        return await begin(db, key=key, user_id=user_id, method=method, path=path, body=body)
    raise err("ERR-SYS-005", details={"reason": "in_flight"})
