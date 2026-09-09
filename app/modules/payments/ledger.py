"""The split engine (decisions #154, #157; plan `07.9-payme-split`). Pure,
synchronous, `Decimal`-only, and never sees a session — the same shape as
`norms/calculator.py`, this module's sibling.

Every configured recipient (`payment_recipients`, decisions #154/#157) takes
a percentage of the payment (floored to the tiyin) or a fixed amount, and
the LESHOZ OF THE CONTOUR RECEIVES WHAT NOBODY TOOK. Nothing configures the
leshoz's own share, which is exactly why the parts sum back to the payment
for every input — the property a silent off-by-one-tiyin bug here would
break first, and the reason flooring is correct where the rest of this
project rounds `ROUND_HALF_UP` (plan `07.9-payme-split` ruling R2): rounding
any part UP can make the configured parts exceed the whole, and there is
nowhere for the excess to come from. `norms` rounds `ROUND_HALF_UP` because
it prices ONE figure; dividing one figure into parts that must still sum
back to it is the opposite problem, and the leshoz is what absorbs the
difference.

Ruling — the ledger is written when money ARRIVES, never when the invoice is
issued: `entries_for_shares` takes a confirmed `ProviderTransaction`, not
just an `Invoice`. An invoice is a claim; the ledger records money that
exists.

`entries_for_shares` writes one `Allocation` per `Share`:
`target=TARGET_RECEIVER` for a configured recipient, `TARGET_RECIPIENT` for
the leshoz's own remainder row (`recipient_id is None`).

**History: this module carried a second, legacy engine (`split`/
`entries_for`) through 2026-09-08.** It split every payment exactly 50/50
between the leshoz (`recipient`) and the state budget (`budget`), the odd
tiyin always landing on the budget half (ruling 12) rather than dropped or
duplicated — the same "the parts must sum back to the whole for every
input" property the current engine restates above, just with a fixed 50%
config of one instead of an arbitrary directory. Stage 7.9 task 2 added the
pair above ADDITIVELY, deliberately leaving the legacy pair in place because
`payments.service.confirm_payment` still called it; task 5 switched that one
caller over and deleted the legacy pair in the same commit, once nothing
called it any more. Only the ONE engine above is live now."""

import uuid
from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal
from typing import NamedTuple

from app.modules.payments.models import (
    ALLOCATION_ENTRY_TYPES,
    TARGET_RECEIVER,
    TARGET_RECIPIENT,
    Allocation,
    Invoice,
    ProviderTransaction,
)

ALLOCATION_ENTRY_PAYMENT = ALLOCATION_ENTRY_TYPES[0]

TIYIN = Decimal("0.01")
HUNDRED = Decimal("100")


class SplitDoesNotFit(ValueError):
    """The configured fixed amounts (plus percentages) exceed the payment, so
    the leshoz's remainder would be negative. Raised rather than clamped: a
    clamp would silently pay a receiver money the citizen never handed
    over."""


class RecipientRule(NamedTuple):
    """One configured line of the split, in the order `split_payment` applies
    it — a `payment_recipients` row, `kind='percent'` or `'fixed'`. The
    leshoz's own share is never fed in as a rule: nothing configures it, and
    `split_payment` computes it itself as the remainder, appended as the
    final `Share` with `recipient_id=None`."""

    recipient_id: uuid.UUID | None
    kind: str
    percent: Decimal | None
    fixed_amount: Decimal | None


class Share(NamedTuple):
    """One resulting line of a split: `recipient_id=None` is always the
    leshoz's own remainder row, produced by `split_payment` itself rather
    than by any `RecipientRule`."""

    recipient_id: uuid.UUID | None
    amount: Decimal


def split_payment(amount: Decimal, rules: Sequence[RecipientRule]) -> list[Share]:
    """`rules` in the order they are applied; the returned list is those
    shares followed by ONE row for the leshoz (`recipient_id=None`) carrying
    the remainder. `sum(shares) == amount` always. An empty `rules` gives the
    leshoz everything, which is the correct reading of "nobody is
    configured", not an error.

    Percentages are floored to the tiyin (`ROUND_FLOOR`), never
    `ROUND_HALF_UP` (plan `07.9-payme-split` ruling R2): rounding any part UP
    can make the configured shares exceed `amount`, and there is nowhere for
    the excess to come from — `norms` rounds `ROUND_HALF_UP` because it
    prices ONE figure, and dividing one figure into parts that must still sum
    back to it is the opposite problem. The leshoz absorbs whatever flooring
    leaves over.

    Raises `SplitDoesNotFit` if the configured shares (percentages plus
    fixed amounts) total more than `amount` — the leshoz's remainder would
    go negative. Never clamped: a clamp would silently pay a receiver money
    the citizen never handed over."""
    shares: list[Share] = []
    taken = Decimal("0.00")
    for rule in rules:
        if rule.kind == "percent":
            assert rule.percent is not None
            part = (amount * rule.percent / HUNDRED).quantize(TIYIN, rounding=ROUND_FLOOR)
        elif rule.kind == "fixed":
            assert rule.fixed_amount is not None
            part = rule.fixed_amount
        else:  # pragma: no cover - the DB CHECK on RECIPIENT_KINDS makes this unreachable
            raise SplitDoesNotFit(f"unknown recipient kind {rule.kind!r}")
        shares.append(Share(rule.recipient_id, part))
        taken += part
    remainder = amount - taken
    if remainder < 0:
        raise SplitDoesNotFit(f"configured shares total {taken} on a payment of {amount}")
    shares.append(Share(None, remainder))
    return shares


def entries_for_shares(
    *,
    invoice: Invoice,
    transaction: ProviderTransaction,
    shares: Sequence[Share],
    accounts: dict[uuid.UUID | None, str | None],
) -> list[Allocation]:
    """One `entry_type="payment"` row per share. `accounts` maps a
    `recipient_id` (`None` for the leshoz) to the account string the caller
    already resolved; a missing key writes `account=None` — it never raises
    and never blocks money that has already arrived.

    Raises `ValueError` if `transaction.invoice_id != invoice.id`: the two
    arguments must already be a matched pair — a stale object reused across
    a retry, or a copy-paste mix-up in the caller, would otherwise produce a
    ledger row pointing at the wrong invoice, silently, with no exception
    and no log line, undetectable until manual reconciliation."""
    if transaction.invoice_id != invoice.id:
        raise ValueError(
            f"transaction {transaction.id} belongs to invoice {transaction.invoice_id}, "
            f"not {invoice.id}"
        )
    return [
        Allocation(
            invoice_id=invoice.id,
            transaction_id=transaction.id,
            recipient_id=share.recipient_id,
            entry_type=ALLOCATION_ENTRY_PAYMENT,
            target=TARGET_RECIPIENT if share.recipient_id is None else TARGET_RECEIVER,
            account=accounts.get(share.recipient_id),
            amount=share.amount,
        )
        for share in shares
    ]
