"""Parser tests (plan `03.10b-payments-reconciliation` task 2). Pure — no `db`
fixture, no session, no I/O beyond the in-memory `bytes` passed in."""

from datetime import date
from decimal import Decimal, InvalidOperation

import pytest

from app.modules.payments.statement_parser import _normalize_amount, parse_csv

MAP = {
    "doc_number": "Док.",
    "amount": "Сумма",
    "operation_date": "Дата",
    "payer_name": "Плательщик",
    "payer_account": "Счёт плательщика",
    "purpose": "Назначение",
}

NBSP = " "
NARROW_NBSP = " "


def _csv(*rows: str) -> bytes:
    header = "Док.,Сумма,Дата,Плательщик,Счёт плательщика,Назначение"
    return ("\r\n".join((header, *rows)) + "\r\n").encode("utf-8-sig")


def test_an_amount_keeps_every_tiyin_and_never_becomes_a_float():
    row = (
        '123,"1234567.89",2026-09-01,ООО Ромашка,20208000000000000001,'
        '"Оплата по счёту INV-2026-000042"'
    )
    lines, errors = parse_csv(_csv(row), column_map=MAP)
    assert errors == []
    assert lines[0].amount == Decimal("1234567.89")
    assert str(lines[0].amount) == "1234567.89"


def test_a_space_separated_amount_with_a_comma_decimal_is_accepted():
    """Uzbek bank exports write 2 060 000,00 — an ordinary space here; the
    NBSP/narrow-NBSP variants a real export also uses are covered directly
    against `_normalize_amount` below, with the actual code points."""
    lines, errors = parse_csv(_csv('124,"2 060 000,00",01.09.2026,X,Y,Z'), column_map=MAP)
    assert errors == []
    assert lines[0].amount == Decimal("2060000.00")
    assert lines[0].operation_date == date(2026, 9, 1)


def test_a_bad_row_is_an_error_and_does_not_stop_the_good_rows():
    lines, errors = parse_csv(
        _csv("125,not-a-number,2026-09-01,X,Y,Z", '126,"100.00",2026-09-02,X,Y,Z'),
        column_map=MAP,
    )
    assert [line.line_no for line in lines] == [3]
    assert [(e.line_no, e.field) for e in errors] == [(2, "amount")]


def test_a_column_map_naming_a_column_the_file_does_not_have_is_one_error_not_many():
    lines, errors = parse_csv(
        _csv('127,"1.00",2026-09-01,X,Y,Z'), column_map={**MAP, "amount": "Сумма!"}
    )
    assert lines == []
    assert len(errors) == 1
    assert errors[0].line_no == 1 and errors[0].field == "amount"


def test_a_column_map_missing_a_required_field_entirely_is_one_error_not_a_crash():
    """`column_map.get()`, not `column_map[...]` — omitting a REQUIRED_FIELDS
    key entirely used to raise `KeyError` instead of producing a `LineError`."""
    incomplete_map = {k: v for k, v in MAP.items() if k != "operation_date"}
    lines, errors = parse_csv(_csv('130,"1.00",2026-09-01,X,Y,Z'), column_map=incomplete_map)
    assert lines == []
    assert len(errors) == 1
    assert errors[0].line_no == 1 and errors[0].field == "operation_date"


def test_bytes_that_decode_as_neither_utf8_nor_cp1251_are_a_line_error_not_an_exception():
    """Byte 0x98 is an invalid UTF-8 start byte AND undefined in cp1251 — both
    decode attempts inside `_decode` fail, and `parse_csv` must still return
    `(lines, errors)`, never let `UnicodeDecodeError` escape."""
    data = b"\x98,1,2026-09-01,X,Y,Z"
    lines, errors = parse_csv(data, column_map=MAP)
    assert lines == []
    assert len(errors) == 1
    assert errors[0].line_no == 1 and errors[0].field == "encoding"


def test_a_row_whose_amount_is_non_finite_or_scientific_is_an_error_not_a_nan_in_the_ledger():
    lines, errors = parse_csv(_csv('131,"nan",2026-09-01,X,Y,Z'), column_map=MAP)
    assert lines == []
    assert [(e.line_no, e.field) for e in errors] == [(2, "amount")]


# --- _normalize_amount, on its own (brief's task-2 step 3: "in one function
# with its own test") ---


def test_normalize_amount_treats_a_comma_as_the_decimal_separator_only_without_a_dot():
    assert _normalize_amount("1234567,89") == Decimal("1234567.89")
    assert _normalize_amount("1,234,567.89") == Decimal("1234567.89")


def test_normalize_amount_accepts_a_real_nbsp_thousands_separator():
    assert _normalize_amount(f"2{NBSP}060{NBSP}000,00") == Decimal("2060000.00")


def test_normalize_amount_accepts_a_narrow_nbsp_thousands_separator():
    assert _normalize_amount(f"2{NARROW_NBSP}060{NARROW_NBSP}000,00") == Decimal("2060000.00")


@pytest.mark.parametrize(
    "bad",
    ["nan", "NaN", "Infinity", "-Infinity", "inf", "-inf", "1e3", "1E3"],
)
def test_normalize_amount_rejects_non_finite_and_scientific_forms(bad):
    """`Decimal()` itself accepts every one of these; a bank statement never
    legitimately writes any of them, and `1e3` is a typo for `1000`, not
    scientific notation — see the module docstring."""
    with pytest.raises(InvalidOperation):
        _normalize_amount(bad)
