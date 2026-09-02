"""Payments — turning an approved application into money received (design/02 §
payments, plan `03.10a-payments-core` task 1). Four tables only: `invoices`,
`payment_intents`, `provider_transactions`, `allocations`. The five tables design/02
also lists under § payments (`manual_payment_confirmations`, `bank_statements`,
`bank_statement_lines`, `reconciliations`, `refunds`) belong to stage 3.10b and are
deliberately absent — do not add them here.

Two corrections against design/02, decided before this task and not re-litigated:

**Ruling P1** — `provider_transactions.invoice_id` is a NOT NULL FK to
`invoices.id`, not the sole `intent_id null` design/02 lists. Payme calls
`PerformTransaction` against `account.id` (the invoice NUMBER, from its own
`CheckPerformTransaction` payload) and may have no `payment_intents` row of ours at
all, so `intent_id` alone would leave some transactions with no path back to an
invoice. `intent_id` stays as a nullable FK beside it, for transactions that DO
originate from one of our own intents. Task 8 records the correction in design/02.

**Ruling P2** — `allocations.refund_id` is omitted entirely: design/02 gives it an
FK to `refunds`, a 3.10b table that does not exist yet, and a nullable FK to a
non-existent table is not expressible. 3.10b adds the column with its FK when it
creates `refunds`. `allocations.transaction_id` (nullable FK to
`provider_transactions`) ships now; the full `entry_type` CHECK (`payment` /
`refund` / `correction`) and the full `target` CHECK (`recipient` / `budget` /
`other`) ship from day one too, the same way 0015 shipped all fourteen application
statuses for writers that do not exist yet.

Every enum-ish column has exactly one source of truth — the module-level tuples
below, each turned into a `CheckConstraint` — mirroring
`app/modules/applications/models.py`. No schemas, service or router in this
branch (Task 2+); nothing here is imported by anything except this module's own
tests and `permissions.py` until then."""

import uuid
from datetime import datetime
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

    `performed_at`/`payload`/`received_at` all default at the schema level (this
    task's own model test never sets them); a real webhook handler (Task 2+) always
    overrides `performed_at`/`payload` from the provider's own payload."""

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
    performed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_provider_transactions_external"),
    )


class Allocation(Base):
    """The ledger of the 50/50 split and of refunds (design/02 § allocations,
    corrected by ruling P2 — `refund_id` omitted, see the module docstring). The
    50/50 proportion itself is enforced in code (Task 3's `payments.ledger`,
    never here); `amount` may be negative (a refund entry) so it carries no
    positivity CHECK.

    `account` is nullable (Task 3 ruling, amending this same unmerged
    migration — see `migrations/versions/0017_payments.py`): the state
    budget's account number is not in the system at all (`tz/08` — the
    budget half is settled by accounting outside the system), and a
    leshoz's `requisites` JSONB may legitimately have no `"account"` key
    (`app/seed/data/organizations.example.json`'s `leshoz-beruniy`). A
    placeholder string in a financial ledger's account column would be
    worse than NULL."""

    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_transactions.id"), index=True
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
