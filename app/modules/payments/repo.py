"""Queries and writes for `payments`. Nothing here decides anything —
idempotency, authorization and the audit trail all belong to `service.py`/
`payme.py`; repo only reads and writes rows (design/01 rule 2: router ->
service -> repo -> models)."""

import uuid
from collections.abc import Collection, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import (
    MANUAL_CONFIRMATION_STATUSES,
    REFUND_STATUSES,
    Allocation,
    BankStatement,
    BankStatementLine,
    Invoice,
    InvoiceRecipient,
    ManualPaymentConfirmation,
    PaymentIntent,
    PaymentRecipient,
    ProviderTransaction,
    Reconciliation,
    Refund,
    RefundComponent,
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


async def has_provider_transaction(db: AsyncSession, invoice_id: uuid.UUID) -> bool:
    """Whether ANY `provider_transactions` row points at `invoice_id` — never
    which one, never how many. `service.is_settled_by_benefit`'s own
    signature (ruling #185): a real payment, Payme's or the manual
    maker-checker door's synthetic `provider='manual'` row alike, ALWAYS
    writes one through `add_provider_transaction` above; `_settle_free`
    never calls it at all, so its absence on a `paid` invoice is exactly
    what tells the two apart."""
    stmt = (
        select(ProviderTransaction.id).where(ProviderTransaction.invoice_id == invoice_id).limit(1)
    )
    return (await db.execute(stmt)).first() is not None


async def add_allocations(db: AsyncSession, allocations: Sequence[Allocation]) -> None:
    """The `ledger.entries_for_shares` rows a confirmed `PerformTransaction`
    writes, one per receiver (`payments.service.confirm_payment`) — plain,
    unattached instances until this call."""
    db.add_all(allocations)
    await db.flush()


async def list_allocations_by_invoice(
    db: AsyncSession, invoice_id: uuid.UUID
) -> Sequence[Allocation]:
    """Every ledger row for one invoice, oldest first (`occurred_at`, then
    `id` as the tie-break — uuid7 is time-ordered) — `payments.service.
    allocations_for` (Task 7), what 4.3's reports and 3.10b's refunds read.
    Unfiltered by `entry_type`: `confirm_payment` writes `"payment"` rows,
    `service.record_reversal` writes the negating `"correction"` rows, and
    the refund register writes `"refund"` rows — all into this SAME table,
    and `record_reversal` itself reads them back through here."""
    stmt = (
        select(Allocation)
        .where(Allocation.invoice_id == invoice_id)
        .order_by(Allocation.occurred_at, Allocation.id)
    )
    return (await db.execute(stmt)).scalars().all()


async def list_allocations(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[Allocation], int]:
    """`GET /payments/allocations` (3.10b task 10) — the whole ledger, oldest
    first, unfiltered by `entry_type` (mirrors `list_allocations_by_invoice`'s
    own reasoning: `payment`, `correction` and `refund` rows all belong in
    the one page a report or an audit reads). Selected either by ONE
    invoice, or by an `occurred_at` window — the ROUTE resolves a
    `period_from`/`period_to` pair into `since`/`until` and guarantees
    exactly one selection mode is given (`ERR-VAL-001` otherwise); this
    function only builds whichever WHERE clause it is handed."""
    stmt = select(Allocation)
    if invoice_id is not None:
        stmt = stmt.where(Allocation.invoice_id == invoice_id)
    if since is not None:
        stmt = stmt.where(Allocation.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(Allocation.occurred_at <= until)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Allocation.occurred_at, Allocation.id).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


# --- The one read this module makes outside its own tables (3.10b task 8) ----
#
# `permits` and `payments` are BOTH level 4, and `design/01` rule 3 forbids one
# calling the other — obeyed in the other direction too (`permits.service.issue`
# reads the APPLICATION's status rather than asking `payments` whether it was
# paid). This is not a call: it is a read-only EXISTS on one table, taken for a
# RISK CHECK — "does a permit already exist for the application whose payment
# just came back", which is `tz/10`'s RI-10 verbatim. It moves nothing, decides
# nothing about a permit, and imports nothing from that module: raw SQL by the
# table name, never `permits.models`.
#
# The alternative was a registered provider seam (the `OCCUPANCY_PROVIDERS`
# shape 3.11a fills for `gis`/`norms`) built for a single boolean read on a rare
# path — more machinery, and one more thing a `workers_mode=off` process can be
# missing, than the fact it would deliver. If that trade is ever re-decided,
# the RI-10 half of `service.record_reversal` is what drops; nothing else here
# depends on this function.


# The permit statuses that make a reversed payment an RI-10, as LITERALS.
#
# `permits.models.PERMIT_STATUSES` is the tuple these three come from, and
# deriving them from it is exactly what this module does with its OWN model
# tuples two functions up. It is not done here, deliberately: importing
# `permits.models` to reach it would be the level-4 import the comment above
# says this function exists to avoid, and a boundary is worth more than a
# derived literal (fix round 1, ruling). `tests/modules/payments/
# test_reversal.py` covers the one status whose absence is load-bearing.
#
# Why these three and not the other three (fix round 1, ruling):
#   - `pending_signatures` COUNTS — the document exists and the money was taken.
#   - `active` COUNTS — the plain reading of «Разрешение активировано без оплаты».
#   - `suspended` COUNTS — suspension is reversible; the permit is still live.
#   - `revoked` does NOT — an operator has already dealt with it, and an RI-10
#     on it is a false positive on a CRITICAL indicator 4.2 harvests by string.
#   - `expired` does NOT — it ran its full course on money that was earned.
#   - `archived` does NOT.
_RI_10_PERMIT_STATUSES = ("pending_signatures", "active", "suspended")

_PERMIT_ORGANIZATION_SQL = text(
    "SELECT organization_id FROM permits WHERE application_id = :application_id"
    " AND status IN :statuses LIMIT 1"
).bindparams(bindparam("statuses", expanding=True))


async def permit_organization_for_application(
    db: AsyncSession, application_id: uuid.UUID
) -> uuid.UUID | None:
    """The `organization_id` of the permit that would make a reversed payment an
    RI-10, if one exists for `application_id` — read-only, see the comment above
    for why this module may ask and which statuses count. `None` when no such
    permit exists, which doubles as the RI-10 EXISTS check itself (ruling #112:
    this organization is also who `record_reversal` notifies, since the leshoz
    that must decide whether to suspend the permit IS the permit's own
    `organization_id` — no second read).

    `LIMIT 1`, not a list: `permits.application_id` is `unique=True`, so at most
    one row can ever match."""
    row = (
        await db.execute(
            _PERMIT_ORGANIZATION_SQL,
            {"application_id": application_id, "statuses": list(_RI_10_PERMIT_STATUSES)},
        )
    ).first()
    return row[0] if row is not None else None


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
    db: AsyncSession,
    application_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Invoice], int]:
    """Every invoice ever raised for `application_id` (not just the in-force
    one) — a cancelled/expired invoice is still part of the application's own
    history, not something a read route should hide. `status`, when given,
    narrows to one of `INVOICE_STATUSES` (backend-gaps review, 2026-09-06):
    before this parameter existed, `GET /invoices?application_id=&status=`
    silently dropped `status` on this branch and returned every invoice for
    the application regardless of it — no error, no hint, the combination
    simply untested."""
    conditions = [Invoice.application_id == application_id]
    if status is not None:
        conditions.append(Invoice.status == status)
    stmt = select(Invoice).where(*conditions)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


async def list_invoices(
    db: AsyncSession, *, status: str | None, limit: int, offset: int
) -> tuple[list[Invoice], int]:
    """The whole register, newest first — `GET /invoices` with no
    `?application_id=` (backend-gaps finding 3): an accountant's screen was a
    lookup box with no way to browse. `invoices` carries no `organization_id`
    of its own (it belongs to a leshoz only through its application), so
    territorial scoping is NOT applied here — `payments.service.
    list_invoices_for_actor` does it per row, the same way `backoffice_
    service.list_manual_confirmations` already does for a table in the same
    position. `status`, when given, narrows to one of `INVOICE_STATUSES`."""
    conditions = [] if status is None else [Invoice.status == status]
    stmt = select(Invoice).where(*conditions)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


async def list_invoices_by_applications(
    db: AsyncSession,
    application_ids: Collection[uuid.UUID],
    *,
    status: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Invoice], int]:
    """A citizen's own page (stage 11, ruling R1): every invoice whose
    application is one of `application_ids` — the owner's set
    `applications.service.owned_application_ids` answers — newest first,
    every status, optionally narrowed to one `INVOICE_STATUSES` member. An
    empty set answers an empty page and issues NO statement
    (`applications.repo.list_application_ids_by_applicants`'s own rule)."""
    if not application_ids:
        return [], 0
    conditions: list[Any] = [Invoice.application_id.in_(list(application_ids))]
    if status is not None:
        conditions.append(Invoice.status == status)
    stmt = select(Invoice).where(*conditions)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


async def list_invoices_matching(db: AsyncSession, *, status: str | None) -> Sequence[Invoice]:
    """Every invoice matching `status` (all of them, if `None`), newest
    first, with NO limit/offset — `payments.service._scan_invoices_in_zone`'s
    own full scan (backend-gaps review, 2026-09-06), not a page: that
    function's own docstring explains why a zone-scoped register read needs
    the whole matching set rather than a capped or paginated one, given
    `invoices` has no zone column of its own to filter or count on in SQL."""
    conditions = [] if status is None else [Invoice.status == status]
    stmt = select(Invoice).where(*conditions).order_by(Invoice.issued_at.desc(), Invoice.id.desc())
    return (await db.execute(stmt)).scalars().all()


# --- 3.10b: bank statements, their lines and the reconciliation register ------


async def add_statement(db: AsyncSession, statement: BankStatement) -> None:
    db.add(statement)
    await db.flush()


async def get_statement(db: AsyncSession, statement_id: uuid.UUID) -> BankStatement | None:
    return await db.get(BankStatement, statement_id)


async def list_statements(
    db: AsyncSession, *, status: str | None, limit: int, offset: int
) -> tuple[list[BankStatement], int]:
    """`GET /payments/bank-statements` with no id (backend-gaps finding 3):
    every imported statement, newest first — headers only, no lines (a list
    row has no use for a per-line page; `GET /payments/bank-statements/{id}`
    is where those live). No zone scoping, matching `get_statement`'s own
    docstring: a bank statement belongs to the accounting department, not to
    a leshoz, and carries no `organization_id` to scope on."""
    conditions = [] if status is None else [BankStatement.status == status]
    stmt = select(BankStatement).where(*conditions)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(BankStatement.created_at.desc(), BankStatement.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows), total


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


async def list_manual_confirmations(
    db: AsyncSession, *, status: str | None
) -> Sequence[ManualPaymentConfirmation]:
    """Every manual confirmation, oldest first — `GET /payments/manual-
    confirmations`, task defect 4b: the maker had no way to hand the checker
    anything but the invoice id by hand. No pagination here: this table
    carries no `organization_id` of its own, so zone scoping happens in the
    SERVICE, per row, through each row's invoice (`backoffice_service`'s own
    zone helper — the same per-row shape decision #70 already established
    for exactly this application-only relationship); the service slices the
    page only after that filter, which a SQL `OFFSET`/`LIMIT` here could not
    honour correctly."""
    stmt = select(ManualPaymentConfirmation)
    if status is not None:
        stmt = stmt.where(ManualPaymentConfirmation.status == status)
    rows = await db.scalars(stmt.order_by(ManualPaymentConfirmation.created_at))
    return list(rows)


# --- 3.10b task 9: refunds -----------------------------------------------


async def add_refund(db: AsyncSession, refund: Refund) -> None:
    db.add(refund)
    await db.flush()


async def add_refund_components(db: AsyncSession, rows: Sequence[RefundComponent]) -> None:
    """The whole breakdown `backoffice_service.submit_refund_decision`
    builds for ONE refund, written together (mirrors `add_invoice_recipients`'
    own shape) — plain, unattached instances until this call."""
    db.add_all(rows)
    await db.flush()


async def list_refund_components(
    db: AsyncSession, refund_id: uuid.UUID
) -> Sequence[RefundComponent]:
    """One refund's breakdown by source — `backoffice_service.approve_refund`'s
    own read of what `submit_refund_decision` already stored, and
    `refunds_router.py`'s read for `available_sources`/`components` on the
    wire. No ordering is guaranteed by the ROWS themselves (unlike
    `InvoiceRecipient.position`); a caller that needs a stable order sorts
    by whatever it reads off each row (e.g. `recipient_id IS NULL last`,
    mirroring the snapshot's own remainder-last convention)."""
    stmt = select(RefundComponent).where(RefundComponent.refund_id == refund_id)
    return (await db.execute(stmt)).scalars().all()


async def get_refund(db: AsyncSession, refund_id: uuid.UUID) -> Refund | None:
    """The plain, non-locking read — `GET /refunds/{id}` (stage 7.9 task 7).
    `get_refund_for_update` below is for the two decision routes, which
    must serialize; a read-only view of the register takes no lock."""
    return await db.get(Refund, refund_id)


async def get_refund_for_update(db: AsyncSession, refund_id: uuid.UUID) -> Refund | None:
    """The locking read for `submit_refund_decision`/`approve_refund` —
    mirrors `get_manual_confirmation_for_update`'s own reasoning: two
    accountants (or an accountant and a rahbar) racing the same refund must
    serialize, or both could read a stale status and both go on to write a
    decision that contradicts the other's."""
    return await db.get(Refund, refund_id, with_for_update=True, populate_existing=True)


async def list_refunds(
    db: AsyncSession,
    *,
    application_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Refund], int]:
    """`GET /refunds` — every refund, newest first, optionally narrowed to
    one application or one status. Both filters are optional and independent
    (mirrors `list_reconciliations`'s own shape, one filter wider)."""
    stmt = select(Refund)
    if application_id is not None:
        stmt = stmt.where(Refund.application_id == application_id)
    if status is not None:
        stmt = stmt.where(Refund.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Refund.requested_at.desc(), Refund.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


# --- 3.10b task 10: the refund SLA sweep --------------------------------------

# Which refund statuses are still "awaiting a decision" for RI-07's purposes
# (mirrors `IN_FORCE_STATUSES` above): a `returned`/`rejected` row is
# terminal and untouched by the sweep, even one long past its own `due_at`.
REFUND_OPEN_STATUSES = (REFUND_STATUSES[0], REFUND_STATUSES[1])  # "requested", "in_review"


async def list_refunds_past_due(db: AsyncSession, *, on_date: date) -> Sequence[Refund]:
    """Every `requested`/`in_review` refund whose 20-working-day control
    deadline (`due_at`, RI-07) is already in the past — `payments.jobs.
    refund_sla_sweep`'s candidate set. A `returned`/`rejected` row is
    excluded by the status filter alone, which is also what makes a second
    sweep run a no-op for a row an earlier run already flagged AND decided
    in between."""
    stmt = select(Refund).where(Refund.status.in_(REFUND_OPEN_STATUSES), Refund.due_at < on_date)
    return (await db.execute(stmt)).scalars().all()


# --- Stage 7.9 task 3: the recipients directory --------------------------------


async def add_payment_recipient(db: AsyncSession, recipient: PaymentRecipient) -> None:
    db.add(recipient)
    await db.flush()


async def get_payment_recipient(
    db: AsyncSession, recipient_id: uuid.UUID
) -> PaymentRecipient | None:
    return await db.get(PaymentRecipient, recipient_id)


async def list_payment_recipients(
    db: AsyncSession, *, limit: int, offset: int
) -> tuple[list[PaymentRecipient], int]:
    """The whole directory, active AND inactive (there is no DELETE, ruling
    #157 — an inactive row stays a first-class citizen of this list forever),
    ordered `(sort_order, id)` — the same tie-break `list_active_recipients`
    below uses, so an admin's list and the engine's own reading order agree."""
    stmt = select(PaymentRecipient)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(PaymentRecipient.sort_order, PaymentRecipient.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows), total


async def list_active_recipients(db: AsyncSession) -> Sequence[PaymentRecipient]:
    """The ACTIVE rows only, `(sort_order, id)` ordered — read DIRECTLY by
    `payments.service.issue_invoice` (task 4, so the split's `rules` AND
    the name/`payme_account_id` each snapshot row copies come from the SAME
    single read) and by the percent-total validator's own read of
    "everything that currently counts". Unpaged: this directory is a
    handful of rows by nature (one line per party who takes a cut off the
    top), never a register that grows with transaction volume."""
    stmt = (
        select(PaymentRecipient)
        .where(PaymentRecipient.active.is_(True))
        .order_by(PaymentRecipient.sort_order, PaymentRecipient.id)
    )
    return (await db.execute(stmt)).scalars().all()


# --- Stage 7.9 task 4: the split frozen onto one invoice ------------------


async def add_invoice_recipients(db: AsyncSession, rows: Sequence[InvoiceRecipient]) -> None:
    """The whole split snapshot `payments.service._snapshot_rows` builds for
    ONE invoice, written together at issuance (decision #158) — `position`
    order, the leshoz's `kind='remainder'` row always last. Plain,
    unattached instances until this call, mirroring `add_allocations`
    above."""
    db.add_all(rows)
    await db.flush()


async def list_invoice_recipients(
    db: AsyncSession, invoice_id: uuid.UUID
) -> Sequence[InvoiceRecipient]:
    """One invoice's frozen split, `position` order — `payments.service.
    invoice_recipients` (Task 4), the ONE way a caller outside this module
    learns how an invoice divides. Never read `payment_recipients` (the LIVE
    directory) for this question instead: that would answer "what applies
    today", not "what this invoice divides into", and defeat the freeze the
    whole table exists for (decision #158)."""
    stmt = (
        select(InvoiceRecipient)
        .where(InvoiceRecipient.invoice_id == invoice_id)
        .order_by(InvoiceRecipient.position)
    )
    return (await db.execute(stmt)).scalars().all()
