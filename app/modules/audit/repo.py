"""Audit repository: DB access for audit_log."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models import AuditLog


async def add(db: AsyncSession, entry: AuditLog) -> None:
    """Stage the entry and flush, so constraint violations surface at the call site."""
    db.add(entry)
    await db.flush()
