"""Audit repository: DB access for audit_log."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


async def add(db: AsyncSession, entry: AuditLog) -> None:
    """Stage the entry and flush, so constraint violations surface at the call site."""
    db.add(entry)
    await db.flush()


async def exists(db: AsyncSession, *, action: str, object_type: str, object_id: uuid.UUID) -> bool:
    """Whether an `audit_log` row already exists for this `(action, object_type,
    object_id)` triple.

    `object_type` is REQUIRED, not optional convenience: `ix_audit_log_object`
    is `(object_type, object_id, occurred_at)`, and PostgreSQL cannot use a
    composite index at all once its LEADING column is missing from the WHERE
    clause — confirmed by `EXPLAIN` against a real database (fix round 1's own
    finding; a first version of this function omitted `object_type` and the
    plan came back `Seq Scan on audit_log`). Supplying it here is what turns
    the same query into an index scan: `action` is filtered in Python-side
    predicate order after the index narrows to one `(object_type, object_id)`
    pair, not the reverse. `audit_log` is append-only and grows forever, so a
    caller that drops this argument reintroduces a full table scan on every
    call, invisibly — nothing short of `EXPLAIN` would show it."""
    stmt = select(AuditLog.id).where(
        AuditLog.object_type == object_type,
        AuditLog.object_id == object_id,
        AuditLog.action == action,
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None
