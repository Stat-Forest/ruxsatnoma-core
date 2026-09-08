"""The configurable split (decisions #154, #157) that replaces the fixed
50/50: `split_payment` is pure arithmetic — no `db` fixture anywhere in this
file, every rule and share below is a plain `NamedTuple`. `ledger.py`'s own
module docstring explains why this pair exists ALONGSIDE the legacy
`split`/`entries_for` rather than instead of it until Task 5."""

import uuid
from decimal import Decimal

import pytest

from app.modules.payments.ledger import (
    RecipientRule,
    Share,
    SplitDoesNotFit,
    split_payment,
)

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
