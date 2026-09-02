"""The four tables of stage 3.10a and the invariants the database itself enforces."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.payments.models import Invoice, ProviderTransaction


async def test_an_application_cannot_have_two_invoices_in_force(db, approved_application):
    """Ruling 10 / design/02: a second approval must not silently issue a second
    invoice for the same application."""
    for _ in range(2):
        db.add(
            Invoice(
                application_id=approved_application.id,
                number=f"INV-2027-{uuid.uuid4().hex[:6]}",
                amount=Decimal("100.00"),
                status="pending",
            )
        )
    with pytest.raises(IntegrityError, match="uq_invoices_one_in_force"):
        await db.flush()


async def test_a_cancelled_invoice_does_not_block_a_new_one(db, approved_application):
    db.add(
        Invoice(
            application_id=approved_application.id,
            number="INV-2027-000001",
            amount=Decimal("100.00"),
            status="cancelled",
        )
    )
    await db.flush()
    db.add(
        Invoice(
            application_id=approved_application.id,
            number="INV-2027-000002",
            amount=Decimal("100.00"),
            status="pending",
        )
    )
    await db.flush()


async def test_the_same_payme_transaction_id_cannot_be_stored_twice(db, invoice):
    """Ruling 5: Payme repeats a call verbatim when it loses our answer. The
    database is the backstop that makes a double-perform impossible."""
    for _ in range(2):
        db.add(
            ProviderTransaction(
                provider="payme",
                external_id="payme-tx-1",
                amount=Decimal("100.00"),
                state="1",
                invoice_id=invoice.id,
            )
        )
    with pytest.raises(IntegrityError, match="uq_provider_transactions_external"):
        await db.flush()
