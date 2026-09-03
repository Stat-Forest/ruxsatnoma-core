"""Payments — turning an approved application into money received (design/02 §
payments, plan `03.10a-payments-core` task 1 and `03.10b-payments-reconciliation`
task 1). Nine tables: 3.10a's `invoices`, `payment_intents`, `provider_transactions`,
`allocations`, and 3.10b's `manual_payment_confirmations`, `bank_statements`,
`bank_statement_lines`, `reconciliations`, `refunds` — the back office, maker-checker
manual PAID, bank reconciliation and refunds.

Two corrections against design/02, decided before 3.10a task 1 and not re-litigated:

**Ruling P1** — `provider_transactions.invoice_id` is a NOT NULL FK to
`invoices.id`, not the sole `intent_id null` design/02 lists. Payme calls
`PerformTransaction` against `account.id` (the invoice NUMBER, from its own
`CheckPerformTransaction` payload) and may have no `payment_intents` row of ours at
all, so `intent_id` alone would leave some transactions with no path back to an
invoice. `intent_id` stays as a nullable FK beside it, for transactions that DO
originate from one of our own intents. Task 8 records the correction in design/02.

**Ruling P2** — `allocations.refund_id` is a nullable FK to `refunds`, added by
3.10b task 1 in the same migration that creates `refunds` (design/02 always gave it
one; 3.10a could not, since `refunds` did not exist yet — see `Allocation`'s own
docstring for the shipped column). `allocations.transaction_id` (nullable FK to
`provider_transactions`) shipped in 3.10a; the full `entry_type` CHECK (`payment` /
`refund` / `correction`) and the full `target` CHECK (`recipient` / `budget` /
`other`) shipped from day one too, the same way 0015 shipped all fourteen application
statuses for writers that do not exist yet.

Task 4 (`payme.py`) amended 3.10a's own migration a third time — see
`ProviderTransaction`'s own docstring, ruling A, for the three columns.

Every enum-ish column has exactly one source of truth — the module-level tuples
below, each turned into a `CheckConstraint` — mirroring
`app/modules/applications/models.py`. No schemas, service or router in this
branch (3.10b task 2+); nothing here is imported by anything except this module's
own tests and `permissions.py` until then."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

INVOICE_STATUSES = ("pending", "paid", "expired", "cancelled")
# payment_intents.provider only — provider_transactions.provider stays unconstrained
# free text (design/02 gives it no enumerated values, unlike payment_intents'):
# the row is the provider's own raw confirmation, and a future provider must be
# storable there before this module has a service that creates intents for it.
PAYMENT_PROVIDERS = ("payme", "manual")
PAYMENT_INTENT_STATUSES = ("created", "pending", "succeeded", "failed", "expired")
ALLOCATION_ENTRY_TYPES = ("payment", "refund", "correction")
ALLOCATION_TARGETS = ("recipient", "budget", "other")

MANUAL_CONFIRMATION_STATUSES = ("pending_check", "confirmed", "rejected")
BANK_STATEMENT_SOURCES = ("api", "file")  # only "file" has a writer yet (ruling 20)
BANK_STATEMENT_FORMATS = ("csv",)  # ruling 9
BANK_STATEMENT_STATUSES = ("pending", "parsing", "parsed", "failed")
# `provider_settlement` is a deliberate FIFTH value beyond design/02's four
# (ruling 10): a Payme payout is one aggregated line standing for many
# invoices, and calling it `unknown_payment` would bury the period's whole
# provider turnover in the accountant's exception register every month.
LINE_MATCH_STATUSES = (
    "unmatched",
    "matched",
    "unknown_payment",
    "discrepancy",
    "provider_settlement",
)
RECONCILIATION_RESULTS = ("matched", "discrepancy", "unknown")
RECONCILIATION_STATUSES = ("open", "resolved")
REFUND_STATUSES = ("requested", "in_review", "returned", "rejected")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    """Renders `column IN ('a', 'b')` directly from the tuple, rather than
    `f"{column} IN {values}"` (every CHECK above this point): Python's tuple
    repr puts a trailing comma before the closing paren of a ONE-element tuple
    (`('csv',)`), and Postgres's `IN` list is a syntax error on that comma —
    `BANK_STATEMENT_FORMATS` is this module's first enum-ish tuple with a
    single member. Used for every 3.10b CHECK below so a value added later
    (making a tuple single-member or not) never silently reintroduces the bug."""
    literal = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({literal})"


class Invoice(Base):
    """The invoice raised for an approved application (design/02 § invoices).

    `uq_invoices_one_in_force` (ruling 10) is the partial unique index below —
    at most one `pending`/`paid` invoice per application at a time; a
    `cancelled`/`expired` one does not block a new one from being issued.

    `issued_at`/`due_at` default to `now()` at the schema level so a bare
    `Invoice(...)` is always insertable (this task's own model tests never set
    them) — the real "+10 days" rule design/02 gives `due_at` is Task 2's
    invoice-creation service's job to compute and pass explicitly; the schema
    only guarantees the column is never NULL.

    `calculation_id` (Task 2) is NOT in design/02 and Task 1 did not create it —
    added so `payments.service.issue_invoice` can freeze exactly which
    `norms.calculations` row it billed, making a later divergence (3.9b's
    recalculate writes a new calculation row) a two-column comparison instead of
    an invisible drift. Nullable: Task 1's own model tests build a bare
    `Invoice(...)` with no calculation at all.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str] = mapped_column(unique=True)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    calculation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calculations.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(default="pending")
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    due_at: Mapped[datetime] = mapped_column(server_default=func.now())
    paid_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"status IN {INVOICE_STATUSES}", name="status_valid"),
        Index(
            "uq_invoices_one_in_force",
            "application_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'paid')"),
        ),
    )


class PaymentIntent(Base):
    """An attempt to pay one invoice through a provider (design/02 §
    payment_intents). `idempotency_key` is this module's own domain key (the value
    a client repeats to avoid opening two intents for one click), distinct from the
    HTTP `Idempotency-Key` mechanism in `app/core/idempotency.py`."""

    __tablename__ = "payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    provider: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(default="created")
    external_ref: Mapped[str | None]
    idempotency_key: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"provider IN {PAYMENT_PROVIDERS}", name="provider_valid"),
        CheckConstraint(f"status IN {PAYMENT_INTENT_STATUSES}", name="status_valid"),
    )


class ProviderTransaction(Base):
    """The provider's own confirmation of a payment (design/02 §
    provider_transactions, corrected by ruling P1 — see the module docstring).
    `uq_provider_transactions_external` (ruling 5) is what makes Payme's verbatim
    retry of a lost call a no-op instead of a second transaction: `external_id` is
    Payme's own transaction `id`, `state` its state (1 / 2 / -1 / -2 — see
    `04-integrations.md` §3), both free text since a provider's own vocabulary is
    not this module's to constrain.

    `payload`/`received_at` default at the schema level (this task's own model
    test never sets them); Task 4's `payme.py` always overrides `payload` from
    Payme's own params and `received_at` from its OWN injected clock (ruling G),
    never the schema default — the 12h timeout is measured from it, and a test
    that froze time would otherwise be measured against the real wall clock the
    server default reads.

    Task 4 ruling A amends this branch's own unmerged migration (0017) with three
    columns design/02 lists none of (Task 8 records the correction there, not
    here):

    - `performed_at` is nullable with NO server default (unlike Task 1's
      original `func.now()`) — a transaction sitting in state `1` must report
      `perform_time: 0` to `CheckTransaction`, not the moment the row was
      created.
    - `cancelled_at` (nullable) — `CheckTransaction`'s `cancel_time`.
    - `cancel_reason` (nullable) — design/04 §3.5's reasons 1-5/10;
      `CancelTransaction` and the 12h-timeout auto-cancel (reason `4`) both
      write it, `CheckTransaction` reads it back unchanged.
    """

    __tablename__ = "provider_transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_intents.id"), index=True
    )
    provider: Mapped[str]
    external_id: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    state: Mapped[str]
    performed_at: Mapped[datetime | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[int | None]

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_provider_transactions_external"),
    )


class Allocation(Base):
    """The ledger of the 50/50 split and of refunds (design/02 § allocations).
    The 50/50 proportion itself is enforced in code (Task 3's
    `payments.ledger`, never here); `amount` may be negative (a refund entry)
    so it carries no positivity CHECK.

    `account` is nullable (Task 3 ruling, amending this same unmerged
    migration — see `migrations/versions/0017_payments.py`): the state
    budget's account number is not in the system at all (`tz/08` — the
    budget half is settled by accounting outside the system), and a
    leshoz's `requisites` JSONB may legitimately have no `"account"` key
    (`app/seed/data/organizations.example.json`'s `leshoz-beruniy`). A
    placeholder string in a financial ledger's account column would be
    worse than NULL.

    `refund_id` (3.10b task 1, ruling P2) is a nullable FK to `refunds`,
    added by migration `0022` in the same transaction that creates that
    table — design/02 always gave this column an FK target, 3.10a simply
    could not express it against a table that did not exist yet."""

    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_transactions.id"), index=True
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refunds.id"), index=True)
    entry_type: Mapped[str]
    target: Mapped[str]
    account: Mapped[str | None]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    note: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(f"entry_type IN {ALLOCATION_ENTRY_TYPES}", name="entry_type_valid"),
        CheckConstraint(f"target IN {ALLOCATION_TARGETS}", name="target_valid"),
    )


class ManualPaymentConfirmation(Base):
    """The one legal way to mark an invoice PAID by hand (design/02 §
    manual_payment_confirmations, `tz/08` §4, `tz/05` invariant 3, ruling 1):
    maker-checker, backed by a real bank document, and it automatically
    raises risk indicator RI-01. `bank_doc_file_id` is NOT NULL — a manual
    PAID without a document is exactly what ruling 1 forbids, so the schema
    makes it unrepresentable rather than merely unwritten by the service.

    `confirmed_needs_checker` is the CHECK that makes a
    `confirmed` row without an independent checker impossible at the
    database level — the same invariant `design/02`'s own table lists —
    never bypassable by a future write path that skips the service layer.
    Money still enters the system through `payments.service.confirm_payment`
    only (3.10b plan, ruling 14): a confirmed row here is the trigger a
    later task uses to synthesize a `provider = "manual"` transaction, not a
    second write path of its own."""

    __tablename__ = "manual_payment_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    paid_at: Mapped[datetime]
    bank_doc_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"), index=True)
    maker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    checker_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(default="pending_check")
    reason: Mapped[str | None]
    checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in_check("status", MANUAL_CONFIRMATION_STATUSES), name="status_valid"),
        CheckConstraint(
            "status <> 'confirmed' OR (checker_id IS NOT NULL AND checker_id <> maker_id)",
            name="confirmed_needs_checker",
        ),
    )


class BankStatement(Base):
    """One imported bank statement (design/02 § bank_statements, 3.10b): the
    header row a later task's parser (`format="csv"` only, ruling 9) fills in
    from an uploaded file — `source="api"` is a value this table already
    supports for a future automatic feed (`tz/08`: "loaded automatically (API)
    or as a file"), but no writer produces it yet (ruling 20). `column_map`
    lets an accountant tell the parser which spreadsheet column is which
    without a code change; `stats`/`error_report` are the later parsing
    task's own summary of what happened to each line."""

    __tablename__ = "bank_statements"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    source: Mapped[str]
    format: Mapped[str]
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    statement_date: Mapped[date]
    period_from: Mapped[date | None]
    period_to: Mapped[date | None]
    column_map: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    imported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(default="pending")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    error_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in_check("source", BANK_STATEMENT_SOURCES), name="source_valid"),
        CheckConstraint(_in_check("format", BANK_STATEMENT_FORMATS), name="format_valid"),
        CheckConstraint(_in_check("status", BANK_STATEMENT_STATUSES), name="status_valid"),
    )


class BankStatementLine(Base):
    """One row of an imported statement (design/02 § bank_statement_lines,
    3.10b). `uq_bank_statement_lines_statement_line` (`statement_id`,
    `line_no`) makes a re-parse of the same statement collide instead of
    doubling the register — the parser's own idempotency key.

    `match_status` starts `"unmatched"` and is the automatic matcher's own
    vocabulary (a later task): `matched` (a clean invoice/transaction hit),
    `unknown_payment` ("in the bank, nothing in the system" — `tz/08`),
    `discrepancy` (matched but the amount disagrees) and
    `provider_settlement`, a deliberate fifth value beyond design/02's four
    (ruling 10) — a Payme payout line stands for many invoices at once, and
    calling it `unknown_payment` would bury the whole period's provider
    turnover in the accountant's exception register every month."""

    __tablename__ = "bank_statement_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    statement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bank_statements.id"), index=True)
    line_no: Mapped[int]
    doc_number: Mapped[str | None]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    operation_date: Mapped[date]
    payer_name: Mapped[str | None]
    payer_account: Mapped[str | None]
    purpose: Mapped[str | None]
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    match_status: Mapped[str] = mapped_column(default="unmatched")
    matched_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoices.id"), index=True
    )
    matched_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_transactions.id"), index=True
    )

    __table_args__ = (
        UniqueConstraint("statement_id", "line_no", name="uq_bank_statement_lines_statement_line"),
        CheckConstraint(_in_check("match_status", LINE_MATCH_STATUSES), name="match_status_valid"),
    )


class Reconciliation(Base):
    """A comparison between the bank and the system — matched, a discrepancy
    (matched but disagreeing) or unknown (design/02 § reconciliations,
    3.10b): the discrepancy register plus a task for the accountant `tz/08`
    describes, closed with a comment or a correcting document
    (`resolution_doc_id`). `ix_reconciliations_open` is the register's own
    query — every open row, oldest first."""

    __tablename__ = "reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    statement_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_statement_lines.id"), index=True
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_transactions.id"), index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    result: Mapped[str]
    difference: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(default="open")
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    comment: Mapped[str | None]
    resolution_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_files.id"), index=True
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    resolved_at: Mapped[datetime | None]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in_check("result", RECONCILIATION_RESULTS), name="result_valid"),
        CheckConstraint(_in_check("status", RECONCILIATION_STATUSES), name="status_valid"),
        Index("ix_reconciliations_open", "status", "occurred_at"),
    )


class Refund(Base):
    """A manual refund and its breakdown by source (design/02 § refunds,
    decision #12, 3.10b). `basis_item_id` points at the seeded
    `refund_reasons` classifier (migration `0022`, ruling 16) — revocation /
    unused period / overpayment / confirmed benefit (VMQ 278 §§9-11), never
    a CHECK-constrained enum column, so a new ground is reference data, not a
    migration. `suggested_amount` is the formula's hint
    (`tz/08`: `Paid × unused_eligible_period / paid_period`) and is nullable
    for ruling 17's degenerate cases (no calculable period); the accountant's
    actual `final_amount` may deviate from it, with a comment.

    `returned_needs_complete_breakdown` is the CHECK design/02 names: a
    `returned` refund without `final_amount` and a `budget`/`recipient`/
    `other` breakdown that sums to it is impossible at the database level —
    the same "returned_amount without a breakdown by source is impossible"
    invariant `design/02`'s own invariants table lists. `due_at` is a plain
    calendar `date` (3.10b task 1 decision), not a `timestamptz`: the 20
    working-day control deadline (RI-07) is a day, not a moment."""

    __tablename__ = "refunds"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    basis_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("classifier_items.id"), index=True)
    suggested_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    suggestion_reason: Mapped[str | None]
    final_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    budget_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    recipient_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    other_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(default="requested")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    requested_at: Mapped[datetime]
    due_at: Mapped[date]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    decided_at: Mapped[datetime | None]
    comment: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(_in_check("status", REFUND_STATUSES), name="status_valid"),
        CheckConstraint(
            "status <> 'returned' OR (final_amount IS NOT NULL AND "
            "coalesce(budget_amount, 0) + coalesce(recipient_amount, 0) "
            "+ coalesce(other_amount, 0) = final_amount)",
            name="returned_needs_complete_breakdown",
        ),
    )
