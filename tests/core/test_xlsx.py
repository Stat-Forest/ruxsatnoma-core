"""Stage 13 (ruling #204): the generic register → .xlsx renderer."""

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from app.core import xlsx

COLUMNS: list[xlsx.Column[SimpleNamespace]] = [
    xlsx.Column("number", {"uz_latn": "Raqam", "ru": "Номер"}, lambda r: r.number),
    xlsx.Column("amount", {"uz_latn": "Summa", "ru": "Сумма"}, lambda r: r.amount),
    xlsx.Column("when", {"uz_latn": "Vaqt", "ru": "Время"}, lambda r: r.when),
    xlsx.Column("day", {"uz_latn": "Kun", "ru": "День"}, lambda r: r.day),
    xlsx.id_column(),
]


def _rows() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            number="RX-1",
            amount=Decimal("12.50"),
            when=datetime(2026, 9, 11, 3, 0, tzinfo=UTC),
            day=date(2026, 9, 11),
            id="u1",
        ),
        SimpleNamespace(number=None, amount=None, when=None, day=None, id="u2"),
    ]


def _sheet(data: bytes):
    sheet = load_workbook(io.BytesIO(data)).active
    assert sheet is not None
    return sheet


def test_render_writes_headers_in_the_requested_language_and_the_id_last():
    sheet = _sheet(xlsx.render(_rows(), COLUMNS, lang="ru", title="Заявки"))
    assert sheet.title == "Заявки"
    assert [c.value for c in sheet[1]] == ["Номер", "Сумма", "Время", "День", "ID"]
    sheet = _sheet(xlsx.render(_rows(), COLUMNS, lang="uz_latn", title="Arizalar"))
    assert [c.value for c in sheet[1]] == ["Raqam", "Summa", "Vaqt", "Kun", "ID"]


def test_render_types_cells_and_converts_datetimes_to_tashkent():
    sheet = _sheet(xlsx.render(_rows(), COLUMNS, lang="uz_latn", title="t"))
    row = [c.value for c in sheet[2]]
    assert row[0] == "RX-1"
    assert row[1] == 12.5  # a number cell, not the string "12.50"
    assert row[2] == datetime(2026, 9, 11, 8, 0)  # UTC+5, naive — Excel has no tz
    assert row[3] == datetime(2026, 9, 11, 0, 0)  # openpyxl reads a date cell back as datetime
    assert row[4] == "u1"
    assert [c.value for c in sheet[3]] == [None, None, None, None, "u2"]


def test_render_caps_the_sheet_title_at_excels_limit():
    sheet = _sheet(xlsx.render([], COLUMNS, lang="ru", title="x" * 40))
    assert sheet.title == "x" * 31


def test_render_refuses_a_column_missing_a_language():
    bad: list[xlsx.Column[SimpleNamespace]] = [
        xlsx.Column("x", {"ru": "X"}, lambda r: ""),
        xlsx.id_column(),
    ]
    with pytest.raises(ValueError, match="uz_latn"):
        xlsx.render([], bad, lang="ru", title="t")


def test_render_refuses_a_column_set_whose_last_column_is_not_the_id():
    with pytest.raises(ValueError, match="id column"):
        xlsx.render([], COLUMNS[:-1], lang="ru", title="t")


def test_xlsx_response_reports_truncation_in_headers():
    resp = xlsx.xlsx_response(b"x", filename="arizalar-2026-09-11.xlsx", total=12340, cap=10000)
    assert resp.media_type == xlsx.MEDIA_TYPE
    assert resp.headers["X-Export-Total"] == "12340"
    assert resp.headers["X-Export-Rows"] == "10000"
    assert resp.headers["X-Export-Truncated"] == "true"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith('attachment; filename="arizalar-2026-09-11.xlsx"')
    assert resp.headers["Access-Control-Expose-Headers"] == (
        "X-Export-Total, X-Export-Rows, X-Export-Truncated"
    )

    ok = xlsx.xlsx_response(b"x", filename="a.xlsx", total=5, cap=10000)
    assert ok.headers["X-Export-Rows"] == "5"
    assert ok.headers["X-Export-Truncated"] == "false"


def test_localized_prefers_the_requested_language_then_falls_back_in_order():
    assert xlsx.localized({"uz_latn": "Lat", "ru": "Ру"}, lang="ru") == "Ру"
    assert xlsx.localized({"uz_latn": "Lat", "ru": "Ру"}, lang="uz_latn") == "Lat"
    assert xlsx.localized({"uz_cyrl": "Кир", "ru": "Ру"}, lang="uz_latn") == "Кир"
    assert xlsx.localized({"ru": "Ру"}, lang="uz_latn") == "Ру"
    assert xlsx.localized({"kaa": "Qq"}, lang="ru") == "Qq"
    assert xlsx.localized({"uz_latn": ""}, lang="uz_latn") == ""
    assert xlsx.localized(None, lang="ru") == ""
