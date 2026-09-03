"""Audit repository: DB access for audit_log."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


async def add(db: AsyncSession, entry: AuditLog) -> None:
    """Stage the entry and flush, so constraint violations surface at the call site."""
    db.add(entry)
    await db.flush()


async def exists(db: AsyncSession, *, action: str, object_id: uuid.UUID) -> bool:
    """Whether an `audit_log` row already exists for this `(action, object_id)`
    pair — `ix_audit_log_object` (`object_type`, `object_id`, `occurred_at`)
    does not cover `action` alone, but `object_id` is selective enough that
    the index still serves this query; a periodic job's once-only check
    (`payments.jobs.refund_sla_sweep`, 3.10b task 10) has no reason to spend
    a second index for it."""
    stmt = select(AuditLog.id).where(AuditLog.action == action, AuditLog.object_id == object_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None
