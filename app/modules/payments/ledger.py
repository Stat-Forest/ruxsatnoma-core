"""Two split engines live here during the migration off the fixed 50/50 split
(decisions #154, #157; plan `07.9-payme-split` ruling R2). Both are pure,
synchronous, `Decimal`-only, and neither ever sees a session — the same shape
as `norms/calculator.py`, this module's sibling.

**The legacy pair — `split`/`entries_for`.** The 50/50 split between the
leshoz (`recipient`) and the state budget (`budget`) that
`payments.service.confirm_payment` still calls today. **It is on its way
out**: Task 5 of plan `07.9-payme-split` switches `confirm_payment` over to
the pair below and deletes this one in the same commit. A reader arriving
mid-branch should not have to guess which pair is live — it is this
docstring that says so, because both pairs otherwise look equally current.

`split`'s one rule, stated so it stays visible: the odd tiyin goes to the
BUDGET half, deliberately (ruling 12) —
`split(Decimal("100.01")) == (Decimal("50.00"), Decimal("50.01"))`. The
property that actually matters is broader than that one case: the two halves
always sum back to the whole, for every input — that is what a silent
off-by-one-tiyin bug here would break first.

Ruling — the ledger is written when money ARRIVES, never when the invoice is
issued: `entries_for` takes a confirmed `ProviderTransaction`, not just an
`Invoice`. An invoice is a claim; the ledger records money that exists. The
same ruling governs `entries_for_shares` below.

**The new pair — `split_payment`/`entries_for_shares`.** Every configured
recipient (`payment_recipients`, decisions #154/#157) takes a percentage of
the payment (floored to the tiyin) or a fixed amount, and the LESHOZ OF THE
CONTOUR RECEIVES WHAT NOBODY TOOK. Nothing configures the leshoz's own
share, which is exactly why the parts sum back to the payment for every
input — the property a silent off-by-one-tiyin bug here would break first,
and the reason flooring is correct where the rest of this project rounds
`ROUND_HALF_UP` (plan `07.9-payme-split` ruling R2): rounding any part UP can
make the configured parts exceed the whole, and there is nowhere for the
excess to come from. `norms` rounds `ROUND_HALF_UP` because it prices ONE
figure; dividing one figure into parts that must still sum back to it is the
opposite problem, and the leshoz is what absorbs the difference.

`entries_for_shares` keeps `entries_for`'s invoice/transaction consistency
check verbatim (`ValueError` when `transaction.invoice_id != invoice.id`) and
writes one `Allocation` per `Share`: `target=TARGET_RECEIVER` for a
configured recipient, `TARGET_RECIPIENT` for the leshoz's own remainder row
(`recipient_id is None`)."""

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


def split(amount: Decimal) -> tuple[Decimal, Decimal]:
    """`(recipient, budget)`: an exact 50/50 split of `amount`, floored to the
    tiyin on the recipient's side so any single odd tiyin lands on `budget`
    instead (ruling 12) — never the reverse, and never dropped."""
    recipient = (amount / 2).quantize(TIYIN, rounding=ROUND_FLOOR)
    budget = amount - recipient
    return recipient, budget


def entries_for(
    *,
    invoice: Invoice,
    transaction: ProviderTransaction,
    recipient_account: str | None,
    budget_account: str | None = None,
) -> list[Allocation]:
    """The two `payment` rows one confirmed `transaction` produces: one
    `target="recipient"`, one `target="budget"`, split from
    `transaction.amount` — never `invoice.amount`, which is only a claim and
    may have been raised against a calculation superseded since. Both rows
    carry `invoice_id`/`transaction_id` so either one traces back to both the
    claim and the money that settled it.

    `recipient_account`/`budget_account` are plain strings the caller already
    resolved; either may be `None` (a leshoz's `requisites` JSONB with no
    `"account"` key, or the budget half, which tz/08 says is settled by
    accounting outside this system and so has none to give at all) — a
    missing account writes the row with `account=None`; it never raises and
    never blocks money that has already arrived.

    Returns two plain, unattached `Allocation` instances — never added to a
    session, never flushed. The caller owns the session and the write.

    Raises `ValueError` if `transaction.invoice_id != invoice.id`: the two
    arguments must already be a matched pair — a stale object reused across
    a retry, or a copy-paste mix-up in the caller, would otherwise produce a
    ledger row pointing at the wrong invoice, silently, with no exception
    and no log line, undetectable until manual reconciliation. Checking it
    costs neither I/O nor a session — both objects are already in memory —
    so it is an internal-consistency check on the caller's own inputs, not a
    business rule, and does not cost this module its purity."""
    if transaction.invoice_id != invoice.id:
        raise ValueError(
            f"transaction {transaction.id} belongs to invoice {transaction.invoice_id}, "
            f"not {invoice.id}"
        )
    recipient_amount, budget_amount = split(transaction.amount)
    return [
        Allocation(
            invoice_id=invoice.id,
            transaction_id=transaction.id,
            entry_type="payment",
            target="recipient",
            account=recipient_account,
            amount=recipient_amount,
        ),
        Allocation(
            invoice_id=invoice.id,
            transaction_id=transaction.id,
            entry_type="payment",
            target="budget",
            account=budget_account,
            amount=budget_amount,
        ),
    ]


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

    Raises `ValueError` if `transaction.invoice_id != invoice.id` — same
    check as `entries_for`, for the same reason: the two arguments must
    already be a matched pair, or a stale/copy-pasted object would produce a
    ledger row pointing at the wrong invoice, silently."""
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
