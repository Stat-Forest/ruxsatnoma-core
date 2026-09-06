"""Control-ratio checks run at `submit` (tz/04 С20: "логические проверки:
суммы, контрольные соотношения"). Plan "scope cuts": a small fixed checker,
not a rule-DSL interpreter — `report_forms.rules` stays descriptive.

2-ilova and 3-ilova share the same tail columns (`forms_seed.py`'s
`_SHARED_TAIL`/period pair), so ONE function checks both by column CODE
rather than branching on `form_code` — a future form with a genuinely
different shape adds its own function here, it does not widen this one with
an `if`.

Pure: no session, no HTTP. Returns a list of violations (empty = pass), the
same "report everything, let the caller decide preview vs refuse" shape
`norms.checks.run_checks` already uses — `service.submit_report` is the only
caller today and always refuses on a non-empty list (there is no preview
route for a report), but keeping the split means a future preview endpoint
costs nothing here.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

# The two money columns every seeded form carries (forms_seed.py's
# `_SHARED_TAIL`). A row missing either is not itself a violation — a
# not-yet-paid permit legitimately has no `paid_amount` yet — only the
# ORDERING between the two, when both are present, is checked.
TOTAL_AMOUNT_COLUMN = "total_amount"
PAID_AMOUNT_COLUMN = "paid_amount"
PERIOD_FROM_COLUMN = "period_from"
PERIOD_TO_COLUMN = "period_to"


def _as_decimal(value: Any) -> Decimal | None:
    """`None` for anything unusable as a bounded amount — including NaN.

    Lesson: `Decimal("NaN")` parses without raising; the ordering comparison
    one line later is what raises `InvalidOperation` (an `ArithmeticError`,
    not caught by a bare `except ValueError` and not turned into a 422 by
    pydantic since this runs well past request parsing). `is_nan()` is
    checked here, inside the same guard as the parse, so every CALLER of this
    function can compare its result with a plain `<`/`>` and never needs its
    own try/except."""
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
        if parsed.is_nan():
            return None
        return parsed
    except InvalidOperation:
        return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def check_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every violation found across `rows` — `[]` means the report may submit.

    Each violation: `{"row_index": int, "code": str, "message": str}`.
    `row_index` is the row's position in `rows` (0-based), so a UI can
    highlight the exact line — never the permit's own id, which not every
    row-shape (a future form) is guaranteed to carry under the same key.
    """
    violations: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        total = _as_decimal(row.get(TOTAL_AMOUNT_COLUMN))
        paid = _as_decimal(row.get(PAID_AMOUNT_COLUMN))
        if total is not None and paid is not None and paid > total:
            violations.append(
                {
                    "row_index": index,
                    "code": "paid_exceeds_total",
                    "message": "paid_amount exceeds total_amount",
                }
            )

        period_from = _as_date(row.get(PERIOD_FROM_COLUMN))
        period_to = _as_date(row.get(PERIOD_TO_COLUMN))
        if period_from is not None and period_to is not None and period_to < period_from:
            violations.append(
                {
                    "row_index": index,
                    "code": "period_reversed",
                    "message": "period_to is before period_from",
                }
            )
    return violations
