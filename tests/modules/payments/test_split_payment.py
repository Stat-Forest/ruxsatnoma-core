"""The configurable split (decisions #154, #157) that replaces the fixed
50/50: `split_payment` is pure arithmetic — no `db` fixture anywhere in this
file, every rule and share below is a plain `NamedTuple`. `ledger.py`'s own
module docstring carries the legacy 50/50 engine's history — stage 7.9
task 5 deleted it and switched `payments.service.confirm_payment` over to
this pair."""

import uuid
from decimal import Decimal

import pytest

from app.modules.payments.ledger import (
    RecipientRule,
    Share,
    SplitDoesNotFit,
    entries_for_shares,
    split_payment,
)
from app.modules.payments.models import Invoice, ProviderTransaction

BUDGET = uuid.uuid4()
AGENCY = uuid.uuid4()
FUND = uuid.uuid4()


def percent(rid, value):
    return RecipientRule(rid, "percent", Decimal(value), None)


def fixed(rid, value):
    return RecipientRule(rid, "fixed", None, Decimal(value))


def test_no_rules_gives_the_leshoz_everything():
    assert split_payment(Decimal("600000.00"), []) == [Share(None, Decimal("600000.00"))]


def test_the_worked_example_from_decision_154():
    shares = split_payment(
        Decimal("600000.00"),
        [percent(BUDGET, "50"), percent(AGENCY, "10"), fixed(FUND, "50000.00")],
    )
    assert shares == [
        Share(BUDGET, Decimal("300000.00")),
        Share(AGENCY, Decimal("60000.00")),
        Share(FUND, Decimal("50000.00")),
        Share(None, Decimal("190000.00")),
    ]


def test_percentages_are_floored_and_the_leshoz_absorbs_the_tiyin():
    # 33.33% of 100.00 is 33.33; three of them leave 0.01 over.
    shares = split_payment(
        Decimal("100.00"),
        [percent(BUDGET, "33.33"), percent(AGENCY, "33.33"), percent(FUND, "33.33")],
    )
    assert [s.amount for s in shares] == [
        Decimal("33.33"),
        Decimal("33.33"),
        Decimal("33.33"),
        Decimal("0.01"),
    ]


@pytest.mark.parametrize("amount", ["0.01", "1.00", "99.99", "100.00", "2200000.00", "123456.78"])
def test_the_parts_always_sum_back_to_the_whole(amount):
    total = Decimal(amount)
    shares = split_payment(
        total, [percent(BUDGET, "50"), percent(AGENCY, "10"), percent(FUND, "0.5")]
    )
    assert sum((s.amount for s in shares), Decimal("0.00")) == total


def test_the_leshoz_row_is_always_last_and_never_negative():
    shares = split_payment(Decimal("100.00"), [percent(BUDGET, "100")])
    assert shares[-1] == Share(None, Decimal("0.00"))


def test_fixed_amounts_larger_than_the_payment_refuse_rather_than_go_negative():
    with pytest.raises(SplitDoesNotFit):
        split_payment(Decimal("40000.00"), [fixed(FUND, "50000.00")])


def test_percent_and_fixed_together_still_fit_or_refuse():
    with pytest.raises(SplitDoesNotFit):
        split_payment(Decimal("100000.00"), [percent(BUDGET, "90"), fixed(FUND, "20000.00")])


def test_a_zero_payment_divides_into_zero_shares_even_under_a_fixed_amount():
    """Ruling #202: an invoice that is lawfully zero — a statutory exemption
    (`science`, `no_tariff_by_law`) or a verified 100 % benefit (#185) —
    still freezes its receiver snapshot (#158), and a configured FIXED
    amount must not turn that into `SplitDoesNotFit`: nothing arrived, so
    every share is zero, the fixed one included, and the invariant
    `sum(shares) == amount` holds at 0. This is the degenerate case, not a
    clamp — no receiver is paid a tiyin the citizen never handed over."""
    shares = split_payment(Decimal("0.00"), [percent(BUDGET, "50"), fixed(FUND, "15000.00")])
    assert shares == [
        Share(BUDGET, Decimal("0.00")),
        Share(FUND, Decimal("0.00")),
        Share(None, Decimal("0.00")),
    ]


def test_a_positive_payment_smaller_than_a_fixed_amount_still_refuses():
    """The zero case above must not widen into a clamp: one tiyin is a real
    payment, and 15 000 does not fit inside it."""
    with pytest.raises(SplitDoesNotFit):
        split_payment(Decimal("0.01"), [fixed(FUND, "15000.00")])


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


def test_entries_for_shares_refuses_a_transaction_that_belongs_to_a_different_invoice():
    """Review round 1, Minor 2: the deleted `entries_for` carried this exact
    guard and a dedicated test for it (both correctly deleted together,
    stage 7.9 task 5, Override 1) — `entries_for_shares` inherited the SAME
    check, and nothing in `tests/` exercised it until now. Catches a stale
    object reused across a retry, or a copy-paste mix-up in the caller:
    without this, a mismatched pair would silently write a ledger row
    pointing at the WRONG invoice — no exception, no log line,
    undetectable until manual reconciliation. Both objects are already in
    memory, so this costs neither I/O nor a session."""
    invoice = _invoice(amount=Decimal("100.00"))
    other_invoice_id = uuid.uuid4()
    transaction = _transaction(invoice_id=other_invoice_id, amount=Decimal("100.00"))

    with pytest.raises(ValueError, match=str(transaction.id)):
        entries_for_shares(
            invoice=invoice,
            transaction=transaction,
            shares=[Share(None, Decimal("100.00"))],
            accounts={},
        )
