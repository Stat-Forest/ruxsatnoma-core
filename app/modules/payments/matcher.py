"""Deciding what one parsed bank-statement line means (plan
`03.10b-payments-reconciliation` task 3, ruling 10). Pure, synchronous,
`Decimal`-only, and it never sees a session — the same shape as `ledger.py`
and `statement_parser.py`, this module's siblings.

**The account is never a matching key.** The state budget's account number is
not stored anywhere in this system (`tz/12` #15) and a leshoz's
`organizations.requisites` JSONB may legitimately have no `"account"` key, so
at least half of every row in the `allocations` ledger carries
`account = NULL`. Reconciliation can therefore prove that money arrived
against an invoice; it cannot prove that either half of the 50/50 split
reached its own account. Matching runs on the invoice number found in the
line's free-text `purpose` and the amount, nothing else — hence `classify`
takes no account parameter at all, an absence that is itself the rule.

Also: Payme money lands in our own cashbox wallet and reaches a leshoz later
as ONE aggregated settlement payout, so a Payme-paid invoice generally has no
statement line of its own — that is what the `provider_settlement` match
status exists for (ruling 10): it is neither a match nor an exception, and
must not flood the accountant's exception register every month.

`classify` never queries anything itself: the caller looks the invoice number
up and passes `invoice_amount`/`invoice_found` in, which is what keeps this
function testable without a database and keeps the "account is never a key"
rule visible as an absence of a parameter rather than something to remember
not to use.

`difference = line.amount - invoice_amount` — **paid MINUS invoiced**: a
negative number means the line underpaid the invoice, a positive number means
it overpaid. A reader must never have to guess the sign of a money delta.

That convention governs every `reconciliations` row that COMPARES a payment
with an invoice — this module's, `statement_service`'s and
`backoffice_service`'s. It is not the only convention in that column:
`service.record_reversal` writes a row that compares nothing and stores the
money that went BACK, positive (fix round 1). Those rows carry a
`transaction_id` and no `statement_line_id`.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.modules.payments.models import LINE_MATCH_STATUSES, RECONCILIATION_RESULTS
from app.modules.payments.statement_parser import ParsedLine

# `INV-{YEAR(4)}-{NUMBER(6+)}`, verified against `app.core.numbers.next_public_number`
# (`f"{prefix}-{on_date.year}-{row.last_value:06d}"`) and
# `app.modules.payments.service.INVOICE_NUMBER_PREFIX = "INV"` — the same shape
# design/03 §"Public numbers" documents (`INV-2026-000123`). `:06d` is a MINIMUM
# width, not a fixed one: the year's 1 000 000th invoice formats as 7 digits, so
# the counter half is `\d{6,}`, not `\d{6}` — a fixed count would silently stop
# matching once a leshoz's yearly volume crosses that boundary. `\b` word
# boundaries so the hit inside free text ("Оплата по счёту INV-2026-000042 от
# 01.09") does not require the number to be the whole field.
INVOICE_NUMBER_RE = re.compile(r"\bINV-\d{4}-\d{6,}\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    match_status: str
    result: str | None
    difference: Decimal | None
    comment: str | None = None


def _outcome(*, match_status: str, result: str | None, difference: Decimal | None) -> MatchOutcome:
    """The one place `MatchOutcome` is built, so every value this module can
    return is checked against `models.LINE_MATCH_STATUSES` /
    `models.RECONCILIATION_RESULTS` rather than retyped and trusted. Real
    `ValueError`s, not `assert` — an `assert` is stripped under `python -O`,
    which would turn a typo'd literal into a value silently written past the
    CHECK constraint that would otherwise have caught it at insert time."""
    if match_status not in LINE_MATCH_STATUSES:
        raise ValueError(f"not a valid match_status: {match_status!r}")
    if result is not None and result not in RECONCILIATION_RESULTS:
        raise ValueError(f"not a valid reconciliation result: {result!r}")
    return MatchOutcome(match_status=match_status, result=result, difference=difference)


def extract_invoice_number(purpose: str | None) -> str | None:
    """The first `INV-YYYY-NNNNNN` (or longer) found inside `purpose`,
    upper-cased, or `None` if the text names none."""
    if purpose is None:
        return None
    match = INVOICE_NUMBER_RE.search(purpose)
    return match.group(0).upper() if match else None


def classify(
    line: ParsedLine,
    *,
    invoice_amount: Decimal | None,
    invoice_found: bool,
    is_provider_settlement: bool,
) -> MatchOutcome:
    """Five branches, checked in this order:

    1. `is_provider_settlement` -> `provider_settlement`, no result, no
       difference — an aggregated Payme payout, reconciled as a period total.
    2. not `invoice_found` -> `unknown_payment` / `unknown` — the caller
       could not find the invoice its number named (or found none to look
       up at all).
    3. `line.purpose` names no invoice number -> `unknown_payment` /
       `unknown`, checked independently of `invoice_found` (ruling 11: two
       leshozes can bill the same sum on the same day, so a line with NO
       invoice number in it is never matched on amount alone, even if the
       caller's amount happens to coincide with some invoice).
    4. `invoice_amount == line.amount` -> `matched` / `matched`, no
       difference.
    5. otherwise -> `discrepancy` / `discrepancy`, with the signed
       `difference`.
    """
    if is_provider_settlement:
        return _outcome(match_status="provider_settlement", result=None, difference=None)

    if not invoice_found or invoice_amount is None:
        return _outcome(match_status="unknown_payment", result="unknown", difference=None)

    if extract_invoice_number(line.purpose) is None:
        return _outcome(match_status="unknown_payment", result="unknown", difference=None)

    if line.amount == invoice_amount:
        return _outcome(match_status="matched", result="matched", difference=None)

    return _outcome(
        match_status="discrepancy",
        result="discrepancy",
        difference=line.amount - invoice_amount,
    )
