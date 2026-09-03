"""Matcher tests (plan `03.10b-payments-reconciliation` task 3). Pure — no `db`
fixture, no session; `line_factory` below builds a plain `ParsedLine`."""

from datetime import date
from decimal import Decimal

import pytest

from app.modules.payments.matcher import classify, extract_invoice_number
from app.modules.payments.statement_parser import ParsedLine


@pytest.fixture
def line_factory():
    def make(
        *,
        line_no: int = 2,
        doc_number: str | None = "1",
        amount: Decimal = Decimal("1.00"),
        operation_date: date = date(2026, 9, 1),
        payer_name: str | None = "X",
        payer_account: str | None = None,
        purpose: str | None = None,
        raw: dict[str, str] | None = None,
    ) -> ParsedLine:
        return ParsedLine(
            line_no=line_no,
            doc_number=doc_number,
            amount=amount,
            operation_date=operation_date,
            payer_name=payer_name,
            payer_account=payer_account,
            purpose=purpose,
            raw=raw or {},
        )

    return make


def test_the_invoice_number_is_found_inside_free_text():
    assert extract_invoice_number("Оплата по счёту INV-2026-000042 от 01.09") == "INV-2026-000042"
    assert extract_invoice_number("оплата inv-2026-000042") == "INV-2026-000042"
    assert extract_invoice_number("Оплата за услуги") is None
    assert extract_invoice_number(None) is None


def test_a_named_invoice_with_the_right_amount_matches(line_factory):
    out = classify(
        line_factory(purpose="счёт INV-2026-000042", amount=Decimal("2060000.00")),
        invoice_amount=Decimal("2060000.00"),
        invoice_found=True,
        is_provider_settlement=False,
    )
    assert (out.match_status, out.result, out.difference) == ("matched", "matched", None)


def test_a_named_invoice_with_the_wrong_amount_is_a_discrepancy_carrying_the_signed_difference(
    line_factory,
):
    out = classify(
        line_factory(purpose="счёт INV-2026-000042", amount=Decimal("2000000.00")),
        invoice_amount=Decimal("2060000.00"),
        invoice_found=True,
        is_provider_settlement=False,
    )
    assert out.match_status == "discrepancy"
    assert out.difference == Decimal("-60000.00")  # paid MINUS invoiced: negative = underpaid


def test_a_number_that_names_no_invoice_we_hold_is_an_unknown_payment(line_factory):
    out = classify(
        line_factory(purpose="счёт INV-2026-999999", amount=Decimal("1.00")),
        invoice_amount=None,
        invoice_found=False,
        is_provider_settlement=False,
    )
    assert (out.match_status, out.result) == ("unknown_payment", "unknown")


def test_a_line_naming_no_invoice_at_all_is_an_unknown_payment_and_is_never_matched_on_amount(
    line_factory,
):
    """Ruling 11: two leshozes can bill the same sum on the same day."""
    out = classify(
        line_factory(purpose="Оплата за услуги", amount=Decimal("2060000.00")),
        invoice_amount=Decimal("2060000.00"),
        invoice_found=True,
        is_provider_settlement=False,
    )
    assert out.match_status == "unknown_payment"


def test_a_provider_settlement_is_neither_matched_nor_an_exception(line_factory):
    """Ruling 10: one aggregated Payme payout stands for many invoices; it is
    reconciled as a period total, not per invoice, and it must not flood the
    accountant's exception register every month."""
    out = classify(
        line_factory(payer_name="ООО PAYME", purpose="Расчёты за период"),
        invoice_amount=None,
        invoice_found=False,
        is_provider_settlement=True,
    )
    assert (out.match_status, out.result) == ("provider_settlement", None)


def test_a_seven_digit_invoice_number_still_matches(line_factory):
    """`:06d` in `core/numbers.py` is a MINIMUM width, not a fixed one — the
    year's 1 000 000th invoice is `INV-2026-1000000` (7 digits), and a `\\d{6}`
    regex would silently stop matching it."""
    assert extract_invoice_number("счёт INV-2026-1000000") == "INV-2026-1000000"
    out = classify(
        line_factory(purpose="счёт INV-2026-1000000", amount=Decimal("5.00")),
        invoice_amount=Decimal("5.00"),
        invoice_found=True,
        is_provider_settlement=False,
    )
    assert (out.match_status, out.result) == ("matched", "matched")
