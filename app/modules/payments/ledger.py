"""The 50/50 split between the leshoz (`recipient`) and the state budget
(`budget`), and the ledger rows a confirmed provider transaction turns into
(design/02 § allocations, plan `03.10a-payments-core` task 3). Pure,
synchronous, `Decimal`-only, and it never sees a session — the same shape as
`norms/calculator.py`, this module's sibling.

Ruling — the ledger is written when money ARRIVES, never when the invoice is
issued: `entries_for` takes a confirmed `ProviderTransaction`, not just an
`Invoice`. An invoice is a claim; the ledger records money that exists.
Nothing calls this yet — its caller is Task 4's `PerformTransaction`, which
owns the session and does the two account lookups this module deliberately
does not (`gis.service.contour_organization` -> `admin.repo.get_organization`
-> `organization.requisites.get("account")`). Importing `gis`/`admin` here
purely for a type hint would put a level-1 ORM model in a pure arithmetic
module for no benefit — `entries_for` takes the two account strings instead.

`split`'s one rule, stated so it stays visible: the odd tiyin goes to the
BUDGET half, deliberately (ruling 12) —
`split(Decimal("100.01")) == (Decimal("50.00"), Decimal("50.01"))`. The
property that actually matters is broader than that one case: the two halves
always sum back to the whole, for every input — that is what a silent
off-by-one-tiyin bug here would break first."""

from decimal import ROUND_FLOOR, Decimal

from app.modules.payments.models import Allocation, Invoice, ProviderTransaction

TIYIN = Decimal("0.01")


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
    session, never flushed. The caller owns the session and the write."""
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
