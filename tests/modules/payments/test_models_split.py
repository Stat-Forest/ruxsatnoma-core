"""Tasks 1 and 7 of stage 7.9: the schema of the configurable payment split
(migrations `0045`/`0046`, decisions #154-#159, #161-#162, plan
`07.9-payme-split` rulings P1/P3).

Three new tables — `payment_recipients` (the directory), `invoice_recipients`
(the split frozen onto an invoice at issuance) and `refund_components` (a
refund's breakdown by source) — plus `allocations.recipient_id`. Mirrors
`test_models.py`/`test_backoffice_models.py`'s own shape: no HTTP, no
service — pure ORM/CHECK-level tests against a real, migrated schema.

**`0045` was purely additive** (`target_valid` widened to add `'receiver'`
while `'budget'` stayed legal, no backfill, no column drops — that
migration's own docstring) — a transitional shape `0046` (task 7) ended:
`'budget'` is gone from `target_valid` for good (every row that carried it
was rewritten onto `'receiver'` first), and the `refund_components_complete`
trigger — which `0045` deliberately left tolerant of a `returned` refund
with ZERO components, because the old three-column CHECK was still what
balanced it — now requires at least one. Both the narrowed CHECK and the
tightened trigger are pinned below."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import (
    Allocation,
    Invoice,
    InvoiceRecipient,
    PaymentRecipient,
    Refund,
    RefundComponent,
)
from tests.modules.payments.test_refunds import rf01 as rf01

# --- PaymentRecipient: kind_valid, rule_matches_kind ------------------------


async def test_percent_row_may_not_carry_a_fixed_amount(db):
    row = PaymentRecipient(
        name={"uz_latn": "Davlat byudjeti"},
        kind="percent",
        percent=Decimal("50.00"),
        fixed_amount=Decimal("1000.00"),
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="rule_matches_kind"):
        await db.flush()


async def test_percent_above_one_hundred_is_impossible(db):
    row = PaymentRecipient(name={"uz_latn": "Nomi"}, kind="percent", percent=Decimal("100.01"))
    db.add(row)
    with pytest.raises(IntegrityError, match="rule_matches_kind"):
        await db.flush()


async def test_percent_of_zero_is_impossible(db):
    """The lower bound (`percent > 0`) — the brief's own two tests only pin
    the upper bound and the both-set case, not this one."""
    row = PaymentRecipient(name={"uz_latn": "Nomi"}, kind="percent", percent=Decimal("0.00"))
    db.add(row)
    with pytest.raises(IntegrityError, match="rule_matches_kind"):
        await db.flush()


async def test_fixed_row_may_not_carry_a_percent(db):
    row = PaymentRecipient(
        name={"uz_latn": "Nomi"},
        kind="fixed",
        fixed_amount=Decimal("1000.00"),
        percent=Decimal("10.00"),
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="rule_matches_kind"):
        await db.flush()


async def test_fixed_amount_of_zero_is_impossible(db):
    row = PaymentRecipient(name={"uz_latn": "Nomi"}, kind="fixed", fixed_amount=Decimal("0.00"))
    db.add(row)
    with pytest.raises(IntegrityError, match="rule_matches_kind"):
        await db.flush()


async def test_kind_outside_percent_or_fixed_is_impossible(db):
    row = PaymentRecipient(name={"uz_latn": "Nomi"}, kind="cash", fixed_amount=Decimal("100.00"))
    db.add(row)
    with pytest.raises(IntegrityError, match="kind_valid"):
        await db.flush()


async def test_fixed_row_is_insertable(db):
    row = PaymentRecipient(
        name={"uz_latn": "Maqsadli jamg'arma"},
        kind="fixed",
        fixed_amount=Decimal("50000.00"),
        payme_account_id="66aa11",
    )
    db.add(row)
    await db.flush()
    assert row.active is True
    assert row.sort_order == 0


# --- InvoiceRecipient: kind_valid, uq_invoice_recipients_position ----------


async def test_invoice_recipient_kind_outside_snapshot_kinds_is_impossible(
    db: AsyncSession, invoice: Invoice
):
    row = InvoiceRecipient(
        invoice_id=invoice.id,
        name={"uz_latn": "Nomi"},
        kind="cash",
        amount=Decimal("100.00"),
        position=0,
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="kind_valid"):
        await db.flush()


async def test_invoice_recipient_remainder_row_needs_no_recipient(
    db: AsyncSession, invoice: Invoice
):
    """The leshoz's own row (`kind='remainder'`, `recipient_id IS NULL`, per
    the model's docstring) is insertable — the directory has no row standing
    for the leshoz itself."""
    row = InvoiceRecipient(
        invoice_id=invoice.id,
        name={"uz_latn": "Leshoz"},
        kind="remainder",
        amount=Decimal("500.00"),
        position=1,
    )
    db.add(row)
    await db.flush()
    assert row.recipient_id is None


async def test_invoice_recipient_position_is_unique_per_invoice(db: AsyncSession, invoice: Invoice):
    for _ in range(2):
        db.add(
            InvoiceRecipient(
                invoice_id=invoice.id,
                name={"uz_latn": "Nomi"},
                kind="fixed",
                fixed_amount=Decimal("100.00"),
                amount=Decimal("100.00"),
                position=0,
            )
        )
    with pytest.raises(IntegrityError, match="uq_invoice_recipients_position"):
        await db.flush()


# --- RefundComponent: amount_positive, uq_refund_components_source --------


@pytest.fixture
async def refund(db: AsyncSession, invoice: Invoice, rf01: uuid.UUID) -> Refund:
    """A bare `requested` refund (mirrors `test_refund_sweep.py`'s own
    `overdue_refund` fixture) — what matters here is that it exists, not how
    it got to `requested`."""
    row = Refund(
        application_id=invoice.application_id,
        invoice_id=invoice.id,
        basis_item_id=rf01,
        status="requested",
        requested_at=datetime.now(UTC),
        due_at=datetime.now(UTC).date(),
    )
    db.add(row)
    await db.flush()
    return row


async def test_refund_component_amount_must_be_positive(db: AsyncSession, refund: Refund):
    row = RefundComponent(refund_id=refund.id, amount=Decimal("0.00"))
    db.add(row)
    with pytest.raises(IntegrityError, match="amount_positive"):
        await db.flush()


async def test_refund_component_source_is_unique_per_refund(db: AsyncSession, refund: Refund):
    """`uq_refund_components_source`: at most one row per `(refund_id,
    recipient_id)`. A real `payment_recipients` row, not `recipient_id=NULL`
    twice — Postgres never treats two NULLs as equal under a plain UNIQUE
    constraint, so that case would not exercise this index at all."""
    recipient = PaymentRecipient(
        name={"uz_latn": "Nomi"}, kind="fixed", fixed_amount=Decimal("1000.00")
    )
    db.add(recipient)
    await db.flush()
    for _ in range(2):
        db.add(
            RefundComponent(refund_id=refund.id, recipient_id=recipient.id, amount=Decimal("50.00"))
        )
    with pytest.raises(IntegrityError, match="uq_refund_components_source"):
        await db.flush()


# --- Allocation.recipient_id + the widened target_valid CHECK -------------


async def test_allocation_target_accepts_receiver(db: AsyncSession, invoice: Invoice):
    """Ruling P1: `'receiver'` is the new value the split engine writes."""
    row = Allocation(
        invoice_id=invoice.id, entry_type="payment", target="receiver", amount=Decimal("500.00")
    )
    db.add(row)
    await db.flush()
    assert row.target == "receiver"


async def test_allocation_target_no_longer_accepts_budget(db: AsyncSession, invoice: Invoice):
    """Migration `0046` (decision #161) narrowed `target_valid` to
    `('recipient', 'other', 'receiver')` — `'budget'` was only ever legal
    during `0045`'s own additive transition, and every row that carried it
    was rewritten onto `'receiver'` before the value was removed from the
    CHECK. The transition is over; a NEW `'budget'` row is a defect, not a
    tolerated legacy shape, and must refuse."""
    row = Allocation(
        invoice_id=invoice.id, entry_type="payment", target="budget", amount=Decimal("500.00")
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="target_valid"):
        await db.flush()


async def test_allocation_recipient_id_may_point_at_the_directory(
    db: AsyncSession, invoice: Invoice
):
    recipient = PaymentRecipient(
        name={"uz_latn": "Nomi"}, kind="fixed", fixed_amount=Decimal("1000.00")
    )
    db.add(recipient)
    await db.flush()
    row = Allocation(
        invoice_id=invoice.id,
        entry_type="payment",
        target="receiver",
        recipient_id=recipient.id,
        amount=Decimal("1000.00"),
    )
    db.add(row)
    await db.flush()
    assert row.recipient_id == recipient.id


# --- refund_components_complete trigger: tightened by migration 0046 -----


async def test_trigger_refuses_a_returned_refund_with_no_components_at_all(
    db: AsyncSession, refund: Refund
):
    """Migration `0046` (decision #162) tightened the trigger: `0045`'s own
    version tolerated a `returned` refund with ZERO components, because the
    old three-column CHECK was still live and no component had ever been
    written by anyone. That transition is over — `refunds` no longer has
    those columns at all, so a `returned` refund with no components is
    unbalanced by construction and must refuse."""
    refund.final_amount = Decimal("500.00")
    refund.status = "returned"
    with pytest.raises(DBAPIError, match="needs at least one component"):
        await db.flush()


async def test_trigger_refuses_once_components_exist_but_do_not_sum_to_final_amount(
    db: AsyncSession, refund: Refund
):
    db.add(RefundComponent(refund_id=refund.id, amount=Decimal("100.00")))
    await db.flush()
    refund.final_amount = Decimal("500.00")
    refund.status = "returned"
    with pytest.raises(DBAPIError, match="do not sum to final_amount"):
        await db.flush()


async def test_trigger_passes_once_components_sum_to_final_amount(db: AsyncSession, refund: Refund):
    db.add(RefundComponent(refund_id=refund.id, amount=Decimal("500.00")))
    await db.flush()
    refund.final_amount = Decimal("500.00")
    refund.status = "returned"
    await db.flush()
    assert refund.status == "returned"
