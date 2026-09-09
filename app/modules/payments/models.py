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
own tests and `permissions.py` until then.

Stage 7.9 (migration `0045`, task 1, plan ruling P1) replaces the hard-coded
50/50 split with a configurable directory: `PaymentRecipient` (who takes a
cut), `InvoiceRecipient` (the split frozen onto one invoice at issuance) and
`RefundComponent` (a refund's breakdown by source, `refunds`' own trigger
`refund_components_complete`). `0045` itself was purely additive —
`Allocation` gained `recipient_id` and `'receiver'` joined
`ALLOCATION_TARGETS` beside the still-legal `'budget'`, and `Refund` kept
its three legacy amount columns and their own CHECK untouched.

**Migration `0046` (task 7, decisions #161-#162) finishes the rewrite.**
Every historical `target='budget'` allocation and every `refunds` row's
three legacy columns were backfilled onto the new shapes and then dropped:
`Refund` no longer has `budget_amount`/`recipient_amount`/`other_amount` or
`returned_needs_complete_breakdown`, `'budget'` is gone from
`ALLOCATION_TARGETS` (there is no `TARGET_BUDGET` constant any more), and
`refund_components_complete` now requires at least one component for a
`returned` refund — see that migration's own docstring for the five-step
order and why decision #161 made rewriting history cheap NOW (no
production data, dev is only demo data) rather than later."""

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
# 'budget' is GONE (decision #161, migration 0046): every row that carried it
# was rewritten onto the seeded budget directory row (`target='receiver'`,
# `recipient_id=BUDGET_RECIPIENT_ID`) and the value removed from this tuple —
# there is no `TARGET_BUDGET` constant any more, and grepping for one is the
# guard against a caller that still thinks there is. `'other'` SURVIVES:
# those rows (none written by any code path any more) name no party this
# directory can represent, and inventing one would be fabricating a
# recipient this migration had no authority to invent.
ALLOCATION_TARGETS = ("recipient", "other", "receiver")
TARGET_RECIPIENT = "recipient"
TARGET_OTHER = "other"
TARGET_RECEIVER = "receiver"

# Mirrors migrations/versions/0045_payment_split.py::BUDGET_RECIPIENT_ID (and
# 0046's own copy) — a migration may not import app code, so each keeps its
# own literal, the same idiom `tests/modules/payments/conftest.py` already
# uses for the identical id. This is the one copy APPLICATION code may
# import: `dashboard.repo.payments_kpi` names the seeded budget row's OWN
# share by this id (decision #154, Override 1 of stage 7.9 task 8) rather
# than by a `target` string — `'budget'` is gone from `ALLOCATION_TARGETS`
# for good, but the seeded row itself, and its stable id, are not.
BUDGET_RECIPIENT_ID = uuid.UUID("0192f2a0-0000-7000-8000-000000000001")

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

# Stage 7.9 (migration 0045): the configurable split's own directory and
# snapshot vocabularies — see PaymentRecipient/InvoiceRecipient below.
RECIPIENT_KINDS = ("percent", "fixed")
SNAPSHOT_KINDS = ("percent", "fixed", "remainder")


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
    """The ledger of the payment split and of refunds (design/02 §
    allocations). The split itself is computed in code (`payments.ledger`,
    never here) — stage 7.9 (decision #154, 2026-09-09) replaced the
    original fixed 50/50 proportion with a configurable receivers
    directory; see this module's own docstring banner for the full
    history. `amount` may be negative (a refund entry) so it carries no
    positivity CHECK.

    `account` is nullable (Task 3 ruling, amending this same unmerged
    migration — see `migrations/versions/0017_payments.py`): a leshoz's own
    `requisites` JSONB may legitimately have no `"account"` key
    (`app/seed/data/organizations.example.json`'s `leshoz-beruniy`), and —
    since stage 7.9 — a configured receiver's row is null STRUCTURALLY,
    never for lack of data: `payment_recipients` identifies a Payme WALLET
    (`payme_account_id`), never a bank account, so this column carries
    nothing for any of them, the seeded state-budget row included. A
    placeholder string in a financial ledger's account column would be
    worse than NULL.

    `refund_id` (3.10b task 1, ruling P2) is a nullable FK to `refunds`,
    added by migration `0022` in the same transaction that creates that
    table — design/02 always gave this column an FK target, 3.10a simply
    could not express it against a table that did not exist yet.

    `recipient_id` (migration `0045`, stage 7.9) is a nullable FK to
    `payment_recipients` — set when `target='receiver'`, NULL for the
    leshoz's own remainder (`target='recipient'`) and for a legacy
    `target='other'` row (no writer produces one any more, but a historical
    row may still carry it — see migration `0046`'s own docstring, which
    rewrote every `target='budget'` row onto `'receiver'` and removed
    `'budget'` from the CHECK entirely)."""

    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_transactions.id"), index=True
    )
    refund_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refunds.id"), index=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_recipients.id"), index=True
    )
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


class PaymentRecipient(Base):
    """One party that takes something off the top of every payment, before the
    leshoz receives the remainder (decisions #154, #157).

    Exactly ONE of `percent`/`fixed_amount` is set, enforced by
    `rule_matches_kind` below rather than by the service: a row carrying both
    has no defined meaning, and nobody reading it later would know which was
    applied first.

    The leshoz is NOT a row here. It is resolved from the application's contour
    and receives what nobody took — which is why the parts always sum back to
    the payment (ruling R2).

    A recipient is DEACTIVATED, never deleted: a deleted row breaks every report
    over a period in which it was paid.
    """

    __tablename__ = "payment_recipients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payme_account_id: Mapped[str | None]
    kind: Mapped[str]
    percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    fixed_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    note: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in_check("kind", RECIPIENT_KINDS), name="kind_valid"),
        CheckConstraint(
            "(kind = 'percent' AND percent IS NOT NULL AND fixed_amount IS NULL "
            "AND percent > 0 AND percent <= 100) OR "
            "(kind = 'fixed' AND fixed_amount IS NOT NULL AND percent IS NULL "
            "AND fixed_amount > 0)",
            name="rule_matches_kind",
        ),
    )


class InvoiceRecipient(Base):
    """The split FROZEN onto one invoice at issuance (decision #158).

    Same reason `invoices.calculation_id` exists: an invoice is issued when the
    application is approved and may be paid days later, and an administrator
    editing the directory in between must not change what an already-issued
    invoice divides into.

    The LAST row of an invoice's snapshot is the leshoz: `kind='remainder'`,
    `recipient_id IS NULL`, `name` and `payme_account_id` both frozen from
    the organization (task 4's `_leshoz_snapshot_fields`, which reads both
    off the same lookup so the two can never drift apart from each other).

    `amount` is this row's share OF THE INVOICE. It is display and `receivers`
    data — NOT what the ledger writes. `confirm_payment` re-runs the frozen
    RULES against `transaction.amount`, the money that actually arrived, which
    a manual under-payment may make smaller (see Task 5).
    """

    __tablename__ = "invoice_recipients"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_recipients.id"))
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payme_account_id: Mapped[str | None]
    kind: Mapped[str]
    percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    fixed_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    position: Mapped[int]

    __table_args__ = (
        CheckConstraint(_in_check("kind", SNAPSHOT_KINDS), name="kind_valid"),
        UniqueConstraint("invoice_id", "position", name="uq_invoice_recipients_position"),
    )


class RefundComponent(Base):
    """One line of a refund's breakdown by source (`tz/08`), replacing the three
    `refunds.{budget,recipient,other}_amount` columns a fixed 50/50 once made
    sufficient — migration `0046` (decision #161) dropped all three, having
    first backfilled every historical row's total into rows here.
    `recipient_id` names a configured `payment_recipients` row (a
    `target='receiver'` allocation, task 7's `approve_refund`); `NULL` means
    the leshoz's own remainder (`target='recipient'`) — there is no
    component for the old `'other'` bucket, which the `0046` backfill folded
    into the leshoz's remainder (see that migration's own docstring).

    **`uq_refund_components_source` does NOT stop two `recipient_id IS NULL`
    rows** — Postgres treats `NULL <> NULL` under a plain UNIQUE constraint,
    so a duplicate leshoz-remainder component (or any other duplicate
    source, NULL included) is refused by the SERVICE
    (`backoffice_service.submit_refund_decision`), not by this index.

    The "components sum to `final_amount`" invariant moved from a row CHECK
    to a trigger on `refunds` (ruling R4) — it now spans rows, which a CHECK
    cannot express. `0045`'s own version of that trigger tolerated a
    `returned` refund with zero components (needed only during the
    transition its own docstring describes); `0046` tightened it to require
    at least one (decision #162) — see that migration's own docstring."""

    __tablename__ = "refund_components"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    refund_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("refunds.id"), index=True)
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_recipients.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("refund_id", "recipient_id", name="uq_refund_components_source"),
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
    # Explicit, shortened FK name: the naming convention's default
    # (`fk_bank_statement_lines_matched_transaction_id_provider_transactions`,
    # 68 bytes) exceeds Postgres's 63-byte identifier limit, which silently
    # truncates and hashes it on CREATE — the same trap already fixed for
    # `ManualPaymentConfirmation`'s CHECK. "provider_tx" here is short for
    # `provider_transactions`, this table's only other FK to it.
    matched_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "provider_transactions.id",
            name="fk_bank_statement_lines_matched_transaction_id_provider_tx",
        ),
        index=True,
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
    """A manual refund (design/02 § refunds, decision #12, 3.10b).
    `basis_item_id` points at the seeded `refund_reasons` classifier
    (migration `0022`, ruling 16) — revocation / unused period /
    overpayment / confirmed benefit (VMQ 278 §§9-11), never a
    CHECK-constrained enum column, so a new ground is reference data, not a
    migration. `suggested_amount` is the formula's hint
    (`tz/08`: `Paid × unused_eligible_period / paid_period`) and is nullable
    for ruling 17's degenerate cases (no calculable period); the accountant's
    actual `final_amount` may deviate from it, with a comment.

    **The breakdown by source is `RefundComponent` rows, not columns here.**
    Until migration `0046` (decision #161) this table carried
    `budget_amount`/`recipient_amount`/`other_amount` and a row CHECK
    (`returned_needs_complete_breakdown`) enforcing their sum; a fixed 50/50
    made three buckets sufficient, and a configurable directory of any size
    does not fit in a fixed column count. The identical "a returned
    refund's breakdown sums to `final_amount`" invariant now lives in the
    `refund_components_complete` TRIGGER on this table (decision #162) —
    a CHECK cannot span rows, which the new shape requires — tightened by
    `0046` to also require at least one component. `due_at` is a plain
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
    status: Mapped[str] = mapped_column(default="requested")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    requested_at: Mapped[datetime]
    due_at: Mapped[date]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    decided_at: Mapped[datetime | None]
    comment: Mapped[str | None]

    __table_args__ = (CheckConstraint(_in_check("status", REFUND_STATUSES), name="status_valid"),)
