"""Parser tests (plan `03.10b-payments-reconciliation` task 2). Pure — no `db`
fixture, no session, no I/O beyond the in-memory `bytes` passed in."""

from datetime import date
from decimal import Decimal

from app.modules.payments.statement_parser import parse_csv

MAP = {
    "doc_number": "Док.",
    "amount": "Сумма",
    "operation_date": "Дата",
    "payer_name": "Плательщик",
    "payer_account": "Счёт плательщика",
    "purpose": "Назначение",
}


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
    """Uzbek bank exports write 2 060 000,00 — non-breaking spaces included."""
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
