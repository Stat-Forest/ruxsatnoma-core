"""Queries and writes for `payments`. Nothing here decides anything —
idempotency, authorization and the audit trail all belong to `service.py`/
`payme.py`; repo only reads and writes rows (design/01 rule 2: router ->
service -> repo -> models)."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Allocation, Invoice, PaymentIntent, ProviderTransaction

# What "in force" means for an invoice (mirrors `uq_invoices_one_in_force`,
# migration 0017): a `cancelled`/`expired` row does not block a new one.
IN_FORCE_STATUSES = ("pending", "paid")


async def add(db: AsyncSession, invoice: Invoice) -> None:
    db.add(invoice)
    await db.flush()


async def get_invoice(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    return await db.get(Invoice, invoice_id)


async def get_invoice_for_update(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    """`get_invoice`'s locking sibling — `payme._perform_transaction` ONLY
    (mirrors `applications.repo.get_application_for_update`'s own reasoning).
    Two different Payme transaction ids could in principle both point at the
    same invoice and both reach `PerformTransaction` concurrently; without
    the lock both could read `status == "pending"` before either writes, and
    both would then confirm the same invoice paid twice. `populate_existing`
    so a caller who already holds this row from an earlier read in the same
    session sees the locked, current value, not a stale cached one."""
    return await db.get(Invoice, invoice_id, with_for_update=True, populate_existing=True)


async def get_invoice_by_number(db: AsyncSession, number: str) -> Invoice | None:
    """Payme's `account.id` (design/04 §3.8) IS the invoice number — what
    `payme._check_invoice_for_payment` (`CheckPerformTransaction`/
    `CreateTransaction`) resolves it against."""
    return (await db.execute(select(Invoice).where(Invoice.number == number))).scalar_one_or_none()


async def get_provider_transaction_by_external_id(
    db: AsyncSession, provider: str, external_id: str
) -> ProviderTransaction | None:
    """Plain (unlocked) read — `payme._check_transaction` ONLY: `CheckTransaction`
    "never mutates" (the brief's own method table), so it must never take a row
    lock either."""
    stmt = select(ProviderTransaction).where(
        ProviderTransaction.provider == provider,
        ProviderTransaction.external_id == external_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_provider_transaction_by_external_id_for_update(
    db: AsyncSession, provider: str, external_id: str
) -> ProviderTransaction | None:
    """The locking sibling of the function above — every OTHER Payme method
    (`CreateTransaction`'s idempotent-replay branch, `PerformTransaction`,
    `CancelTransaction`) reads the transaction through this one instead, so a
    genuine retry racing a slow first attempt for the SAME Payme `id`
    serialises instead of both reading state `1` and both writing the ledger
    (mirrors `applications.repo.get_application_for_update`)."""
    stmt = (
        select(ProviderTransaction)
        .where(
            ProviderTransaction.provider == provider,
            ProviderTransaction.external_id == external_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def add_provider_transaction(db: AsyncSession, transaction: ProviderTransaction) -> None:
    db.add(transaction)
    await db.flush()


async def add_allocations(db: AsyncSession, allocations: Sequence[Allocation]) -> None:
    """The two `ledger.entries_for` rows a confirmed `PerformTransaction`
    writes (`payments.service.confirm_payment`) — plain, unattached instances
    until this call."""
    db.add_all(allocations)
    await db.flush()


async def list_allocations_by_invoice(
    db: AsyncSession, invoice_id: uuid.UUID
) -> Sequence[Allocation]:
    """Every ledger row for one invoice, oldest first (`occurred_at`, then
    `id` as the tie-break — uuid7 is time-ordered) — `payments.service.
    allocations_for` (Task 7), what 4.3's reports and a future refund
    (3.10b) read. Unfiltered by `entry_type`: today `confirm_payment` only
    ever writes `"payment"` rows, but 3.10b's refunds/corrections land in
    this SAME table."""
    stmt = (
        select(Allocation)
        .where(Allocation.invoice_id == invoice_id)
        .order_by(Allocation.occurred_at, Allocation.id)
    )
    return (await db.execute(stmt)).scalars().all()


async def add_payment_intent(db: AsyncSession, intent: PaymentIntent) -> None:
    db.add(intent)
    await db.flush()


async def list_provider_transactions_in_period(
    db: AsyncSession, provider: str, since: datetime, until: datetime
) -> list[tuple[ProviderTransaction, str]]:
    """`GetStatement`'s own reader (`payme._get_statement` ONLY) — every
    transaction whose OWN `received_at` (never Payme's own `time` param, the
    same clock the 12h timeout uses) falls within `[since, until]`, joined
    to its invoice for the `account.id` the wire response carries alongside
    it. Plain (unlocked): `GetStatement` never mutates."""
    stmt = (
        select(ProviderTransaction, Invoice.number)
        .join(Invoice, ProviderTransaction.invoice_id == Invoice.id)
        .where(
            ProviderTransaction.provider == provider,
            ProviderTransaction.received_at >= since,
            ProviderTransaction.received_at <= until,
        )
        .order_by(ProviderTransaction.received_at)
    )
    rows = await db.execute(stmt)
    return [(transaction, number) for transaction, number in rows.all()]


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


async def list_invoices_past_due(db: AsyncSession, *, now: datetime) -> Sequence[Invoice]:
    """Every `pending` invoice whose 10-day window has already closed —
    `payments.jobs.expiry_sweep`'s candidate set for the INVOICED ->
    EXPIRED_UNPAID transition. A `paid`/`cancelled`/already-`expired` row is
    excluded by the status filter alone, which is also what makes a second
    sweep run a no-op for a row the first run already closed."""
    stmt = select(Invoice).where(Invoice.status == "pending", Invoice.due_at < now)
    return (await db.execute(stmt)).scalars().all()


async def list_invoices_due_soon(
    db: AsyncSession, *, now: datetime, before: datetime
) -> Sequence[Invoice]:
    """Every `pending` invoice due in `[now, before]` — not yet overdue (that
    is `list_invoices_past_due`'s own set) but inside the reminder window
    `payments.jobs.expiry_sweep` notifies on."""
    stmt = select(Invoice).where(
        Invoice.status == "pending", Invoice.due_at >= now, Invoice.due_at <= before
    )
    return (await db.execute(stmt)).scalars().all()


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
