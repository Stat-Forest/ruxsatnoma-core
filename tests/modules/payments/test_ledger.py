"""The 50/50 split and the two-row ledger `entries_for` builds from a confirmed
transaction (plan `03.10a-payments-core` task 3). Pure and synchronous — no `db`
fixture anywhere in this file; every `Invoice`/`ProviderTransaction` below is a
plain, unattached ORM instance, never flushed."""

import uuid
from decimal import Decimal

import pytest

from app.modules.payments import ledger
from app.modules.payments.models import Allocation, Invoice, ProviderTransaction


def test_an_even_amount_splits_exactly():
    assert ledger.split(Decimal("100.00")) == (Decimal("50.00"), Decimal("50.00"))


def test_an_odd_tiyin_goes_to_the_budget_half():
    """Ruling 12: design/04 §3.7 names the 1-tiyin remainder. Assign it
    deliberately, or two halves silently fail to add up to the whole."""
    recipient, budget = ledger.split(Decimal("100.01"))
    assert (recipient, budget) == (Decimal("50.00"), Decimal("50.01"))
    assert recipient + budget == Decimal("100.01")


@pytest.mark.parametrize("amount", ["0.01", "0.03", "1.00", "412000.00", "999999.99"])
def test_the_halves_always_add_back_to_the_whole(amount):
    """The property that matters: money is never created or destroyed by the split."""
    recipient, budget = ledger.split(Decimal(amount))
    assert recipient + budget == Decimal(amount)
    assert recipient >= 0 and budget >= 0


def test_split_never_returns_a_float():
    """A float here loses tiyin on large sums, silently and irreversibly."""
    for value in ledger.split(Decimal("123456.78")):
        assert isinstance(value, Decimal)


def _invoice(*, amount: Decimal) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=amount,
    )


def _transaction(*, invoice_id: uuid.UUID, amount: Decimal) -> ProviderTransaction:
    return ProviderTransaction(
        id=uuid.uuid4(),
        invoice_id=invoice_id,
        provider="payme",
        external_id=f"payme-{uuid.uuid4().hex[:8]}",
        amount=amount,
        state="2",
    )


def test_entries_for_returns_the_two_payment_rows():
    """The invoice's own `amount` is a claim, possibly stale — only
    `transaction.amount` (what actually arrived) may reach the split. Set them
    to different values here so a wrong-amount-source bug cannot pass by luck."""
    invoice = _invoice(amount=Decimal("999.99"))
    transaction = _transaction(invoice_id=invoice.id, amount=Decimal("100.01"))

    entries = ledger.entries_for(
        invoice=invoice,
        transaction=transaction,
        recipient_account="20208000900123456789",
        budget_account="23402810000000000001",
    )

    assert len(entries) == 2
    assert all(isinstance(entry, Allocation) for entry in entries)
    by_target = {entry.target: entry for entry in entries}
    assert set(by_target) == {"recipient", "budget"}

    recipient_entry = by_target["recipient"]
    assert recipient_entry.entry_type == "payment"
    assert recipient_entry.account == "20208000900123456789"
    assert recipient_entry.amount == Decimal("50.00")
    assert recipient_entry.invoice_id == invoice.id
    assert recipient_entry.transaction_id == transaction.id

    budget_entry = by_target["budget"]
    assert budget_entry.entry_type == "payment"
    assert budget_entry.account == "23402810000000000001"
    assert budget_entry.amount == Decimal("50.01")
    assert budget_entry.invoice_id == invoice.id
    assert budget_entry.transaction_id == transaction.id


def test_entries_for_tolerates_a_missing_recipient_account():
    """A leshoz missing its bank details (no `account` key in `requisites`) must
    never block money that has already arrived: the row is written with
    `account=None`, not raised over. The budget side is unaffected."""
    invoice = _invoice(amount=Decimal("100.00"))
    transaction = _transaction(invoice_id=invoice.id, amount=Decimal("100.00"))

    entries = ledger.entries_for(
        invoice=invoice,
        transaction=transaction,
        recipient_account=None,
        budget_account="23402810000000000001",
    )

    by_target = {entry.target: entry for entry in entries}
    assert by_target["recipient"].account is None
    assert by_target["budget"].account == "23402810000000000001"
