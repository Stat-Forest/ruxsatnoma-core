"""Queries and writes for `payments`. Nothing here decides anything —
idempotency, authorization and the audit trail all belong to `service.py`/
`payme.py`; repo only reads and writes rows (design/01 rule 2: router ->
service -> repo -> models)."""

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import (
    MANUAL_CONFIRMATION_STATUSES,
    Allocation,
    BankStatement,
    BankStatementLine,
    Invoice,
    ManualPaymentConfirmation,
    PaymentIntent,
    ProviderTransaction,
    Reconciliation,
)

# What "in force" means for an invoice (mirrors `uq_invoices_one_in_force`,
# migration 0017): a `cancelled`/`expired` row does not block a new one.
IN_FORCE_STATUSES = ("pending", "paid")


async def add(db: AsyncSession, invoice: Invoice) -> None:
    db.add(invoice)
    await db.flush()


async def get_invoice(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    return await db.get(Invoice, invoice_id)


async def get_invoice_for_update(db: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    """`get_invoice`'s locking sibling (mirrors `applications.repo.
    get_application_for_update`'s own reasoning). Two callers, and they are
    the whole list — everything else on `invoices` reads through
    `get_invoice`/`get_in_force_invoice`:

    - `payme._perform_transaction`. Two different Payme transaction ids
      could in principle both point at the same invoice and both reach
      `PerformTransaction` concurrently; without the lock both could read
      `status == "pending"` before either writes, and both would then
      confirm the same invoice paid twice.
    - `jobs._expire_one_invoice`. Not for a race between two sweeps (only
      one runs) but for the LOCK ORDER: the sweep and `PerformTransaction`
      both touch an invoice and its application, and taking them in
      opposite orders is an ABBA deadlock on an overdue invoice being paid
      right now. Both take the invoice here first, then the application
      through `applications.service.set_status` (`jobs.py`'s own module
      docstring carries the full reasoning). It also gives the sweep the
      re-read it needs to skip an invoice paid since its unlocked scan.

    `populate_existing` so a caller who already holds this row from an
    earlier read in the same session sees the locked, current value, not a
    stale cached one — which is what makes that re-read meaningful."""
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
    """The provider's own turnover over a window — every transaction whose OWN
    `received_at` (never Payme's own `time` param, the same clock the 12h
    timeout uses) falls within `[since, until]`, joined to its invoice for the
    `account.id` the wire response carries alongside it. Plain (unlocked):
    neither caller mutates.

    Two callers, and neither may grow a query of its own instead:
    `payme._get_statement` (Payme's `GetStatement`, which this was written for)
    and `statement_service._period_reconciliation` (3.10b), which compares a
    bank payout against this same total. A second, subtly different turnover
    query is exactly how a reconciliation comes to disagree with what we already
    reported to the provider."""
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


async def get_in_force_invoice_for_update(
    db: AsyncSession, application_id: uuid.UUID
) -> Invoice | None:
    """`get_in_force_invoice`'s locking sibling — `payments.service.
    cancel_invoice_for_application` ONLY (mirrors `get_invoice_for_update`'s
    own reasoning; shaped like `get_provider_transaction_by_external_id_
    for_update` rather than that function, since the lookup key here is
    `application_id`, not the invoice's own primary key, so this has to be a
    `select()` with `with_for_update()`, not a locking `db.get`).

    That handler is the `APPLICATION_CANCELLED` subscriber, so it runs
    already inside a transaction that holds the APPLICATION row lock
    (`applications.service.set_status`, called before the event is
    published) — this is what gives it the invoice lock too, so its refusal
    guard (`invoice.status != "pending"`) reads the freshly locked row
    instead of racing a `PerformTransaction` on an unlocked read that gets
    overwritten blind at flush.

    `populate_existing` for the same reason `get_invoice_for_update` needs
    it: a caller who already holds this row from an earlier unlocked read
    (`invoice_for_application`) must see the locked, current value, not a
    stale cached one."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.application_id == application_id,
            Invoice.status.in_(IN_FORCE_STATUSES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


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


# --- 3.10b: bank statements, their lines and the reconciliation register ------


async def add_statement(db: AsyncSession, statement: BankStatement) -> None:
    db.add(statement)
    await db.flush()


async def get_statement(db: AsyncSession, statement_id: uuid.UUID) -> BankStatement | None:
    return await db.get(BankStatement, statement_id)


async def claim_pending_statement(db: AsyncSession) -> BankStatement | None:
    """Claim the oldest `pending` statement; the row lock is held until the
    caller commits or rolls back.

    The same idiom as `gis.repo.claim_pending_import` and
    `integrations.repo.pick_due`: `FOR UPDATE SKIP LOCKED` lets any number of
    worker processes drain the queue without two of them ever taking the same
    statement. Deliberately NOT the outbox — the outbox carries messages
    LEAVING the system, an imported statement is inbound work."""
    return (
        await db.execute(
            select(BankStatement)
            .where(BankStatement.status == "pending")
            .order_by(BankStatement.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def add_statement_lines(db: AsyncSession, lines: Sequence[BankStatementLine]) -> None:
    """Insert a whole statement's lines in one flush — which also populates
    every row's `id`, so the reconciliation rows that point at them can be
    built afterwards without a flush per line."""
    db.add_all(lines)
    await db.flush()


async def add_reconciliations(db: AsyncSession, rows: Sequence[Reconciliation]) -> None:
    db.add_all(rows)
    await db.flush()


async def list_statement_lines(
    db: AsyncSession, statement_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[BankStatementLine], int]:
    """One statement's lines in file order — `GET /payments/bank-statements/{id}`.
    Ordered by `line_no`, which `uq_bank_statement_lines_statement_line` makes
    unique within a statement, so the paging is stable."""
    stmt = select(BankStatementLine).where(BankStatementLine.statement_id == statement_id)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(BankStatementLine.line_no).offset(offset).limit(limit))
    ).scalars()
    return list(rows), total


async def list_invoice_candidates(
    db: AsyncSession, *, amount: Decimal, since: date, until: date, limit: int
) -> list[Invoice]:
    """Invoices that agree with an unmatched bank line on AMOUNT and were issued
    inside `[since, until)` — ruling 11's hint, and nothing more than a hint.

    Deliberately NOT a matching query: the caller writes what this returns into
    `reconciliations.comment` for an accountant to read, and never into
    `matched_invoice_id`. Two leshozes can bill the same sum on the same day, so
    amount and date can only ever suggest. Ordered newest-first — the likeliest
    candidate for a payment that just arrived — and always bounded, since one
    round sum («2 000 000,00») can legitimately be on hundreds of invoices."""
    stmt = (
        select(Invoice)
        .where(
            Invoice.amount == amount,
            Invoice.issued_at >= since,
            Invoice.issued_at < until,
        )
        .order_by(Invoice.issued_at.desc(), Invoice.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


# --- 3.10b task 5: the discrepancy register -----------------------------------


async def get_reconciliation(
    db: AsyncSession, reconciliation_id: uuid.UUID
) -> Reconciliation | None:
    return await db.get(Reconciliation, reconciliation_id)


async def get_reconciliation_for_update(
    db: AsyncSession, reconciliation_id: uuid.UUID
) -> Reconciliation | None:
    """`resolve_reconciliation`'s own locking read — the one writer this table
    has: two accountants (or an accidental double-click) closing the same row
    at once must serialize instead of both succeeding, one of them silently
    overwriting the other's comment (mirrors `get_invoice_for_update`)."""
    return await db.get(
        Reconciliation, reconciliation_id, with_for_update=True, populate_existing=True
    )


async def list_reconciliations(
    db: AsyncSession, *, status: str, limit: int, offset: int
) -> tuple[list[Reconciliation], int]:
    """The register itself, oldest first — `ix_reconciliations_open`
    (`status`, `occurred_at`) is exactly this query's own index."""
    stmt = select(Reconciliation).where(Reconciliation.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Reconciliation.occurred_at, Reconciliation.id).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


# --- 3.10b tasks 6-7: the maker-checker manual payment confirmation ----------

# What "still awaiting a decision" means for a manual confirmation. Unpacked
# from the model's own tuple rather than retyped, the same way
# `backoffice_service` unpacks `RECONCILIATION_STATUSES`.
PENDING_CHECK_STATUS = MANUAL_CONFIRMATION_STATUSES[0]


async def add_manual_confirmation(
    db: AsyncSession, confirmation: ManualPaymentConfirmation
) -> None:
    db.add(confirmation)
    await db.flush()


async def get_manual_confirmation_for_update(
    db: AsyncSession, confirmation_id: uuid.UUID
) -> ManualPaymentConfirmation | None:
    """The checker's own locking read: two `payments.confirm` holders (or one
    double-clicked button) deciding the same filing at once must serialize,
    or both could read `pending_check` and both go on to synthesize a
    transaction for the same invoice. `uq_provider_transactions_external`
    would then abort the second with an IntegrityError 500 rather than the
    `ERR-PAY-004` a human can read — the lock is what makes the status check
    above it mean anything (mirrors `get_invoice_for_update`)."""
    return await db.get(
        ManualPaymentConfirmation, confirmation_id, with_for_update=True, populate_existing=True
    )


async def get_pending_manual_confirmation(
    db: AsyncSession, invoice_id: uuid.UUID
) -> ManualPaymentConfirmation | None:
    """The one `pending_check` filing standing against `invoice_id`, if any —
    the maker's "no second filing while one awaits a decision" guard. A
    `confirmed`/`rejected` row is terminal and does not block a fresh
    filing, which is why this filters on the status rather than counting
    rows."""
    return (
        await db.scalars(
            select(ManualPaymentConfirmation)
            .where(
                ManualPaymentConfirmation.invoice_id == invoice_id,
                ManualPaymentConfirmation.status == PENDING_CHECK_STATUS,
            )
            .order_by(ManualPaymentConfirmation.created_at)
            .limit(1)
        )
    ).first()
