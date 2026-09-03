"""Parsing one bank statement CSV into rows the matcher can reason about
(plan `03.10b-payments-reconciliation` task 2, ruling 9). Pure, synchronous,
`Decimal`-only, and it never sees a session — the same shape as `ledger.py`,
this module's sibling, and as `gis/importer.py`, its structural twin.

**There is no contracted bank statement format** (`tz/09` line 4 says only
"statement file"; `design/04` covers four systems and none of them is a bank),
so the format variance is absorbed by an explicit per-import
`{our field: the file's column name}` map rather than guessed at here — the
same answer `gis` reached for the Agency's truncated Excel headers. A real
bank format, when one is finally delivered, is a second `format` value and a
second function in this file, not a rewrite.

Amounts are read with `Decimal(str)` after normalisation and NEVER through
`float`: 1234567.89 is not representable in binary, and a tiyin lost here is
a tiyin lost in a financial ledger.

`REQUIRED_FIELDS = ("amount", "operation_date", "purpose")` — `doc_number`,
`payer_name` and `payer_account` are optional. `purpose` is the only matching
key the system has (the account is never one — see `matcher.py`'s docstring),
so a statement imported without it can never match anything, and importing it
silently would waste an accountant's day; a file missing any of these three
columns is refused outright, at line 1, rather than accepted with a column
that will never be usable.
"""

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

REQUIRED_FIELDS: tuple[str, ...] = ("amount", "operation_date", "purpose")
OPTIONAL_FIELDS: tuple[str, ...] = ("doc_number", "payer_name", "payer_account")

# Tried in order; the first one that parses the whole string wins (step 3's spec).
_DATE_FORMATS: tuple[str, ...] = ("%Y-%m-%d", "%d.%m.%Y")

# Characters a bank export uses in place of an ordinary space around thousands
# groups: U+00A0 (NBSP) and U+202F (narrow NBSP), plus the ordinary space.
_SPACE_CHARS: tuple[str, ...] = (" ", " ", " ")


@dataclass(frozen=True, slots=True)
class ParsedLine:
    line_no: int
    doc_number: str | None
    amount: Decimal
    operation_date: date
    payer_name: str | None
    payer_account: str | None
    purpose: str | None
    raw: dict[str, str]


@dataclass(frozen=True, slots=True)
class LineError:
    line_no: int
    field: str
    message: str


def _normalize_amount(text: str) -> Decimal:
    """`"1 234 567,89"` / `"2 060 000,00"` (NBSP included) / `"1234567.89"` ->
    `Decimal`. Comma is the decimal separator only when there is a comma and NO
    dot in the string; otherwise commas are thousands separators and are
    stripped. Never goes through `float`."""
    cleaned = text.strip()
    for ch in _SPACE_CHARS:
        cleaned = cleaned.replace(ch, "")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    return Decimal(cleaned)


def _parse_date(text: str) -> date:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date format: {text!r}")


def _decode(data: bytes) -> str:
    """Banks ship `utf-8-sig` (BOM) most often, but also plain `cp1251`."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251")


def _optional_value(
    row: Mapping[str, str], column_map: Mapping[str, str], field_name: str
) -> str | None:
    """`row[column_map[field_name]]`, or `None` when the map has no column for
    this optional field, or that column is empty. A plain function rather than
    a closure over the parse loop's `row` — a nested function capturing a loop
    variable is evaluated late, against whatever `row` holds when it is
    finally CALLED, not defined; taking `row` as a parameter sidesteps that."""
    column = column_map.get(field_name)
    if column is None:
        return None
    value = row.get(column)
    return value if value else None


def parse_csv(
    data: bytes, *, column_map: Mapping[str, str]
) -> tuple[list[ParsedLine], list[LineError]]:
    """Parse one bank statement CSV. `column_map` maps our field names
    (`REQUIRED_FIELDS` plus `OPTIONAL_FIELDS`) to the file's own column
    headers — see the module docstring for why this is a parameter rather
    than a guess.

    Returns `(lines, errors)`. `line_no` is the physical line number, header
    counted as line 1. A column named in `column_map` but absent from the
    file's header is ONE `LineError` at `line_no=1` and an empty result — the
    whole file is unusable, not just one row. Otherwise, one bad field in one
    row is one `LineError` and that row is skipped; every other row is still
    parsed (the same "report every bad row, keep going" shape as
    `gis/importer.py`).
    """
    text = _decode(data)
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []

    for our_field in REQUIRED_FIELDS:
        column = column_map[our_field]
        if column not in fieldnames:
            return [], [
                LineError(
                    line_no=1,
                    field=our_field,
                    message=f"column {column!r} not found in statement header",
                )
            ]

    lines: list[ParsedLine] = []
    errors: list[LineError] = []
    for physical_line, row in enumerate(reader, start=2):
        line_errors: list[LineError] = []

        amount: Decimal | None = None
        amount_text = row.get(column_map["amount"], "")
        try:
            amount = _normalize_amount(amount_text or "")
        except (InvalidOperation, ValueError):  # fmt: skip
            line_errors.append(
                LineError(
                    line_no=physical_line,
                    field="amount",
                    message=f"not a valid amount: {amount_text!r}",
                )
            )

        operation_date: date | None = None
        date_text = row.get(column_map["operation_date"], "")
        try:
            operation_date = _parse_date(date_text or "")
        except ValueError:
            line_errors.append(
                LineError(
                    line_no=physical_line,
                    field="operation_date",
                    message=f"not a valid date: {date_text!r}",
                )
            )

        if line_errors:
            errors.extend(line_errors)
            continue

        assert (
            amount is not None and operation_date is not None
        )  # both branches above raised or set it

        lines.append(
            ParsedLine(
                line_no=physical_line,
                doc_number=_optional_value(row, column_map, "doc_number"),
                amount=amount,
                operation_date=operation_date,
                payer_name=_optional_value(row, column_map, "payer_name"),
                payer_account=_optional_value(row, column_map, "payer_account"),
                purpose=_optional_value(row, column_map, "purpose"),
                raw=dict(row),
            )
        )

    return lines, errors
