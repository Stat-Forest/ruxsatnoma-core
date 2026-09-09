"""The refund formula's hint, and the breakdown-by-source arithmetic check
(`tz/08`, decision #12, plan `03.10b-payments-reconciliation` task 9). Pure,
synchronous, `Decimal`-only, and it never sees a session — the same shape as
`payments.ledger` and `norms.calculator`, this module's siblings.

A refund is a MANUAL process (decision #12): the money moves outside this
system. What this module computes is a HINT for the accountant — never a
figure the system pays out on its own — and `breakdown_is_complete`, the
same arithmetic `refunds.returned_needs_complete_breakdown` enforces at the
database level, checked here FIRST so a caller answers `ERR-VAL-001` rather
than an IntegrityError 500 from that CHECK.

**A hint is never an error** (ruling 17, `backoffice_service.request_refund`'s
own docstring carries the full list of degenerate cases this module's
`hint()` does NOT have to handle itself — a missing calculation, an
unparseable date, a reversed or zero-length period are all screened out
BEFORE this module is called at all, so `hint()`'s only precondition is
`period_to >= period_from`). Every one of those degenerate cases still
files the refund, with `RefundHint(amount=None, reason="...")` instead of a
raised exception."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

TIYIN = Decimal("0.01")


class RefundHint(NamedTuple):
    """What `backoffice_service.request_refund` stores as `suggested_amount`/
    `suggestion_reason`. Exactly one of the two is ever meaningful:
    `amount` is set (and `reason` is `None`) for every period `hint()` can
    price, `0.00` included (`period_to` already past is a VALUE, not a
    failure); `reason` is set (and `amount` is `None`) for every degenerate
    case the caller screens out before ever calling `hint()`."""

    amount: Decimal | None
    reason: str | None


def hint(*, paid: Decimal, period_from: date, period_to: date, on_date: date) -> Decimal:
    """`tz/08`'s formula: `paid × unused_eligible_period / paid_period`,
    rounded `ROUND_HALF_UP` to the tiyin (never Python's default
    `ROUND_HALF_EVEN` — `CLAUDE.md`'s money rule, mirrored from
    `norms.calculator`).

    Both the paid period and the unused share are counted in CALENDAR days,
    INCLUSIVE at both ends: a period `[period_from, period_to]` is
    `(period_to - period_from).days + 1` days long, and the days already
    used are everything strictly before `on_date` — `on_date` itself, and
    every day after it up to `period_to`, count as unused. `on_date` before
    `period_from` (the refund is requested before the permit's period even
    starts) counts zero days used, never a negative number, so the whole
    paid amount comes back; `on_date` at or after `period_to` (the period is
    already over) counts every day used and hints `0.00`, not a negative
    figure — a period already fully consumed refunds nothing, and that is a
    value, not a failure (ruling 17).

    **Precondition: `period_to >= period_from`** (a positive-length period).
    A reversed or zero-length period is one of the degenerate cases the
    CALLER screens out — see this module's own docstring and
    `backoffice_service.request_refund` — and must never reach this
    function; it is not re-checked here, the same way
    `ledger.entries_for_shares` trusts its own caller's invariant rather
    than re-deriving it."""
    total_days = (period_to - period_from).days + 1
    used_days = (on_date - period_from).days
    used_days = max(0, min(used_days, total_days))
    unused_days = total_days - used_days
    return (paid * Decimal(unused_days) / Decimal(total_days)).quantize(
        TIYIN, rounding=ROUND_HALF_UP
    )


def breakdown_is_complete(
    final_amount: Decimal,
    budget_amount: Decimal | None,
    recipient_amount: Decimal | None,
    other_amount: Decimal | None,
) -> bool:
    """Mirrors `refunds.returned_needs_complete_breakdown` (the database
    CHECK) exactly, so `backoffice_service` can run the SAME arithmetic in
    code, ahead of any write: `coalesce(budget, 0) + coalesce(recipient, 0)
    + coalesce(other, 0) == final`. `None` reads as `0.00`, the same
    `coalesce` the CHECK itself uses — a component the accountant leaves
    unset is "nothing from this source", not "unknown"."""
    zero = Decimal("0.00")
    total = (budget_amount or zero) + (recipient_amount or zero) + (other_amount or zero)
    return total == final_amount
