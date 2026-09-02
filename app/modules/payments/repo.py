"""Queries and writes for `payments`. Nothing here decides anything —
idempotency, authorization and the audit trail all belong to `service.py`;
repo only reads and writes rows (design/01 rule 2: router -> service -> repo
-> models)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Invoice

# What "in force" means for an invoice (mirrors `uq_invoices_one_in_force`,
# migration 0017): a `cancelled`/`expired` row does not block a new one.
IN_FORCE_STATUSES = ("pending", "paid")


async def add(db: AsyncSession, invoice: Invoice) -> None:
    db.add(invoice)
    await db.flush()


async def get_invoice(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    return await db.get(Invoice, invoice_id)


async def get_in_force_invoice(db: AsyncSession, application_id: uuid.UUID) -> Invoice | None:
    """The `pending`/`paid` invoice for `application_id`, or `None`. At most
    one such row can ever exist — `uq_invoices_one_in_force` (migration 0017)
    guarantees it — so `scalar_one_or_none` is safe."""
    return (
        await db.execute(
            select(Invoice).where(
                Invoice.application_id == application_id,
                Invoice.status.in_(IN_FORCE_STATUSES),
            )
        )
    ).scalar_one_or_none()


async def list_invoices_by_application(
    db: AsyncSession, application_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Invoice], int]:
    """Every invoice ever raised for `application_id` (not just the in-force
    one) — a cancelled/expired invoice is still part of the application's own
    history, not something a read route should hide."""
    stmt = select(Invoice).where(Invoice.application_id == application_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total
