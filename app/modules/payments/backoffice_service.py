"""The discrepancy register's own service half (plan
`03.10b-payments-reconciliation` task 5): listing the open (or resolved)
`reconciliations` rows Task 4's import writes, and closing one.

**There is no task table, and this stage creates none.** `tz/08` asks for
"a task for the accountant" beside the register; an OPEN `reconciliations`
row with `assigned_to` set IS that task — exactly what `design/02` gives the
column for. Do not build a second worklist on top of this one.

**Resolving never touches money.** Nothing here writes `invoices`,
`allocations` or `provider_transactions` — `statement_service.py`'s own
docstring says why (`tz/05` invariant 3: only a provider confirmation or the
maker-checker path pays an invoice). Closing a row here records that an
accountant looked at a discrepancy and explains it; it does not resolve it
financially.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import MediaFile
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.notifications import service as notifications_service
from app.modules.payments import events as payment_events
from app.modules.payments import repo
from app.modules.payments import service as payments_service
from app.modules.payments.models import (
    INVOICE_STATUSES,
    MANUAL_CONFIRMATION_STATUSES,
    PAYMENT_PROVIDERS,
    RECONCILIATION_RESULTS,
    RECONCILIATION_STATUSES,
    ManualPaymentConfirmation,
    ProviderTransaction,
    Reconciliation,
)

RESOLVE_ACTION = "reconciliation.resolve"

# Unpacked rather than retyped: the one comparison and the one assignment
# below read this name, never a bare `"resolved"` string of their own
# (module docstring's own vocabulary rule).
_, _STATUS_RESOLVED = RECONCILIATION_STATUSES


async def list_reconciliations(
    db: AsyncSession,
    *,
    status: str,
    limit: int,
    offset: int,
    actor: Any,
) -> tuple[Sequence[Reconciliation], int]:
    """`GET /payments/reconciliations` — the register, oldest first.

    `actor` is accepted (and not used to filter) for symmetry with
    `resolve_reconciliation` and for a future personal worklist ("my own
    assigned rows") `tz/08` does not ask for today; the route's own
    `PAYMENTS_VIEW` gate already decides who may call this at all, the same
    way `statement_service.get_statement` needs no actor for an unscoped
    read (neither table carries an `organization_id` to scope on)."""
    return await repo.list_reconciliations(db, status=status, limit=limit, offset=offset)


async def _assert_doc_active(db: AsyncSession, file_id: uuid.UUID, *, reason: str) -> None:
    """An EXISTENCE check, not a validity check (lesson) — confirms a
    `media_files` row exists and is not archived; nothing about whether it
    actually documents this discrepancy. Mirrors `norms.service.
    _assert_doc_active`/`gis.service._assert_approval_doc_active`, each a
    private helper of a sibling module this one may not import.

    `reason` names WHICH document was missing (`resolution_doc_not_active`,
    `bank_doc_not_active`): both callers answer the same `ERR-VAL-001`, and
    without it a client could not tell a stale correcting document from a
    stale bank payment order."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": reason})


async def resolve_reconciliation(
    db: AsyncSession,
    reconciliation_id: uuid.UUID,
    *,
    comment: str,
    resolution_doc_id: uuid.UUID | None,
    actor: Any,
) -> Reconciliation:
    """`POST /payments/reconciliations/{id}/resolve` — `tz/08`: close a
    discrepancy with a comment or with a correcting document.

    A blank comment (`""`, whitespace-only) is `ERR-VAL-001`: the schema's
    own `str` requirement only rules out a MISSING field, not one filled with
    spaces. An already-`resolved` row is `ERR-PAY-005` (409) rather than a
    silent second resolution overwriting the first accountant's comment —
    NOT `ERR-PAY-004`, whose registered message names an invoice, never a
    reconciliation.
    """
    stripped = comment.strip()
    if not stripped:
        raise err("ERR-VAL-001", details={"reason": "comment_required"})
    row = await repo.get_reconciliation_for_update(db, reconciliation_id)
    if row is None:
        raise err("ERR-SYS-003")
    if row.status == _STATUS_RESOLVED:
        raise err("ERR-PAY-005")
    if resolution_doc_id is not None:
        await _assert_doc_active(db, resolution_doc_id, reason="resolution_doc_not_active")

    old_value = {"status": row.status, "comment": row.comment}
    row.status = _STATUS_RESOLVED
    row.comment = stripped
    row.resolution_doc_id = resolution_doc_id
    row.resolved_by = actor.id
    row.resolved_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action=RESOLVE_ACTION,
        user_id=actor.id,
        object_type="reconciliation",
        object_id=row.id,
        old_value=old_value,
        new_value={
            "status": row.status,
            "comment": row.comment,
            "resolution_doc_id": str(resolution_doc_id) if resolution_doc_id else None,
        },
        basis=stripped,
    )
    return row


# --- Tasks 6-7: the maker-checker manual PAID --------------------------------
#
# `tz/05` invariant 3 says an invoice becomes PAID only on a payment
# provider's confirmation. `tz/08` §4 gives exactly ONE exception: a manual
# confirmation under maker-checker, backed by a stored bank document, which
# automatically raises risk indicator RI-01. This is that exception, and
# there is no other.
#
# **The money still enters through `payments.service.confirm_payment`**
# (ruling 14). The checker's approval synthesizes a `provider="manual"`
# `ProviderTransaction` and hands it to that one function unchanged — the
# same call `payme._perform_transaction` makes. Nothing here writes an
# `Allocation`, moves an application, or publishes `payment_confirmed` on its
# own: a second money path is exactly what a maker-checker exception must not
# become.
#
# **The filing pays nothing** (ruling 4). RI-01 fires when the invoice
# actually becomes PAID (`tz/10`: «PAID без подтверждения провайдера/банка»),
# which is the CHECKER's step — a filed confirmation touches no invoice at
# all.

FILE_MANUAL_CONFIRMATION_ACTION = "payment.manual_confirmation_filed"
MANUAL_CONFIRM_ACTION = "payment.manual_confirm"
MANUAL_REJECT_ACTION = "payment.manual_confirm_reject"

# `tz/10`'s indicator for a PAID that no provider confirmed. Written as a
# `result="success"` audit row, NEVER a denial: the action succeeded and is
# legal, and the indicator only says a human should look. This is the
# OPPOSITE of 3.11a's RI-10, which IS a denial and therefore uses decision
# #40's early-commit-then-raise pattern — do not copy that shape here.
RISK_INDICATOR_MANUAL_PAID = payments_service.RISK_INDICATOR_UNCONFIRMED_PAID
"""Aliased, not a second literal (3.10b task 8): `service.record_reversal`
raises the SAME `tz/10` code for the other way an invoice can be `paid`
without a standing provider confirmation — a post-perform reversal. Two
literals would let one door's code be renamed while the other kept the old
one, and `oversight` (4.2) harvests them by string."""

# The invoice status a manual confirmation may be filed against and checked
# on, spelled once and DERIVED from the model's own tuple (`INVOICE_STATUSES`
# is what `status_valid` is built from). Anything else — `paid` (needs no
# manual confirmation), `expired`/`cancelled` (must not gain money by this
# door) — is `ERR-PAY-004`. The tests pin the resulting literals, so a
# reordering of the tuple goes red here rather than silently changing which
# invoices are payable by hand.
_INVOICE_PAYABLE = INVOICE_STATUSES[0]

_STATUS_PENDING_CHECK, _STATUS_CONFIRMED, _STATUS_REJECTED = MANUAL_CONFIRMATION_STATUSES
_, _RESULT_DISCREPANCY, _ = RECONCILIATION_RESULTS

# The synthetic transaction's own constants (ruling 14). `state="2"` is
# Payme's PERFORMED state, reused verbatim rather than given a manual-only
# vocabulary: `provider_transactions.state` has no CHECK and every existing
# reader (`allocations`, 4.3's reports) already reads `"2"` as "the money
# arrived".
# Derived, never retyped: `provider_valid` is a real CHECK built from this
# same tuple, so a literal here could disagree with the database and fail as
# an IntegrityError 500 on live money. The unpack is deliberate over an index
# — adding a third provider breaks this line loudly, at the one place that
# has to decide what the new provider means for a manual confirmation.
_PAYME_PROVIDER, MANUAL_PROVIDER = PAYMENT_PROVIDERS
# `provider_transactions.state` has no CHECK and no tuple to derive from
# (design/02 gives it none — the column stores the provider's own raw state).
# "2" is Payme's PERFORMED, reused verbatim rather than given a manual-only
# vocabulary: every existing reader already reads it as "the money arrived".
MANUAL_TRANSACTION_STATE = "2"


class FiledConfirmation(NamedTuple):
    """What `file_manual_confirmation` answers with.

    The row alone cannot carry `amount_matches_invoice` — it is a comparison
    against `invoices.amount`, not a column — and the ROUTER may not read
    `invoices` to compute it (router -> service -> repo). So the service,
    which already holds the invoice, returns both. Same shape as
    `list_reconciliations`' own `(rows, total)`.
    """

    confirmation: ManualPaymentConfirmation
    amount_matches_invoice: bool


async def file_manual_confirmation(
    db: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    amount: Decimal,
    paid_at: datetime,
    bank_doc_file_id: uuid.UUID,
    actor: Any,
) -> FiledConfirmation:
    """`POST /payments/manual-confirmations` — the MAKER's half: an
    accountant files that money arrived by bank transfer, with the payment
    order behind it.

    **This pays nothing** (ruling 4). The invoice is not touched, no ledger
    row is written and no RI is raised; the filing only creates a
    `pending_check` row for a `payments.confirm` holder to decide on.

    Four checks, in this order:

    1. the invoice exists (`ERR-SYS-003`) and is `pending` (`ERR-PAY-004`) —
       a `paid` invoice needs no manual confirmation, and an
       `expired`/`cancelled` one must not gain money by this door;
    2. `bank_doc_file_id` names an ACTIVE `media_files` row — an EXISTENCE
       check, not a claim that the document proves anything;
    3. no `pending_check` confirmation already stands for this invoice
       (`ERR-PAY-004`), so two filings can never race to two synthetic
       transactions against one invoice. A `rejected` one is terminal and
       does not block a fresh filing.

    A `amount` that disagrees with `invoice.amount` is ACCEPTED and recorded
    (ruling 5) — an underpayment is a real thing an accountant confirms and
    then reconciles — but flagged twice: on the response, and as an OPEN
    `reconciliations` row carrying the difference, so the discrepancy lands
    in the same register Task 4's bank import fills.
    """
    invoice = await repo.get_invoice(db, invoice_id)
    if invoice is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if invoice.status != _INVOICE_PAYABLE:
        raise err("ERR-PAY-004", details={"status": invoice.status})
    await _assert_doc_active(db, bank_doc_file_id, reason="bank_doc_not_active")
    if await repo.get_pending_manual_confirmation(db, invoice_id) is not None:
        raise err("ERR-PAY-004", details={"reason": "manual_confirmation_pending"})

    row = ManualPaymentConfirmation(
        invoice_id=invoice.id,
        amount=amount,
        paid_at=paid_at,
        bank_doc_file_id=bank_doc_file_id,
        maker_id=actor.id,
        status=_STATUS_PENDING_CHECK,
    )
    await repo.add_manual_confirmation(db, row)

    matches = amount == invoice.amount
    if not matches:
        # `difference` is paid MINUS invoiced on every row that COMPARES a
        # payment with an invoice (`matcher.py`'s own convention), so an
        # underpayment is negative and an overpayment positive — the same sign
        # a bank line would get. That is this row.
        #
        # It is not the only convention in the column (fix round 1): a
        # post-perform reversal (`service.record_reversal`) compares nothing
        # and stores the money that went back, positive. Those rows carry a
        # `transaction_id` with no `statement_line_id`, and their `comment`
        # says "reversed a confirmed payment of ..." — a query or a reader
        # that must tell the two apart has both.
        await repo.add_reconciliations(
            db,
            [
                Reconciliation(
                    invoice_id=invoice.id,
                    result=_RESULT_DISCREPANCY,
                    difference=amount - invoice.amount,
                    status=RECONCILIATION_STATUSES[0],
                    assigned_to=actor.id,
                    comment=(
                        f"manual confirmation {row.id}: bank document says {amount}, "
                        f"invoice {invoice.number} is {invoice.amount}"
                    ),
                )
            ],
        )

    await audit.log(
        db,
        action=FILE_MANUAL_CONFIRMATION_ACTION,
        user_id=actor.id,
        object_type="manual_payment_confirmation",
        object_id=row.id,
        new_value={
            "invoice_id": str(invoice.id),
            "amount": str(amount),
            "bank_doc_file_id": str(bank_doc_file_id),
            "amount_matches_invoice": matches,
        },
    )
    return FiledConfirmation(confirmation=row, amount_matches_invoice=matches)


async def check_manual_confirmation(
    db: AsyncSession,
    confirmation_id: uuid.UUID,
    *,
    approve: bool,
    reason: str | None,
    actor: Any,
) -> ManualPaymentConfirmation:
    """`POST /payments/manual-confirmations/{id}/confirm` and `/reject` — the
    CHECKER's half, and the only place in this system where an invoice
    becomes `paid` without a payment provider saying so.

    The order below is load-bearing:

    1. load the confirmation; 404 if absent, `ERR-PAY-004` if it is not
       `pending_check` (already decided, or decided by someone else while
       this request waited on the row lock);
    2. **`actor.id != confirmation.maker_id`, BEFORE any write** (ruling 13)
       — so a maker checking their own filing reads a clean `ERR-ACL-001`
       instead of an IntegrityError 500 from the `confirmed_needs_checker`
       database CHECK. It applies to `sys_admin` too: `require_permission`
       lets a superuser past the permission code, but whose two pairs of
       eyes saw this money is not a permission question. It applies to
       `reject` as well — a maker withdrawing their own filing is still one
       person deciding alone;
    3. lock the INVOICE (`repo.get_invoice_for_update`). **Invoice first,
       then the application** — the one lock order this codebase has
       (`jobs.py`'s own "Lock order" section); `confirm_payment` takes the
       application lock through `applications.service.set_status`;
    4. re-check that the invoice is still `pending` — a Payme payment may
       have landed while this filing waited for a checker;
    5. synthesize the `provider="manual"` `ProviderTransaction` (ruling 14).
       `external_id = str(confirmation.id)`, which is unique BY
       CONSTRUCTION: a bank document's own number is not unique across
       banks and belongs in `payload`, while
       `uq_provider_transactions_external` on `(provider, external_id)`
       makes a double-confirm impossible at the database level as well as
       at the status level;
    6. `service.confirm_payment` — unchanged, not widened, not copied. It
       does the whole job: invoice -> paid, the 50/50 ledger, application ->
       PAID, the applicant told, `payment_confirmed` on the bus;
    7. stamp the confirmation `confirmed`/`rejected` with the checker and
       the moment — all three fields in ONE assignment, since
       `confirmed_needs_checker` forbids a `confirmed` row with a NULL
       `checker_id` even between two flushes of the same transaction;
    8. audit it. A confirm carries `extra={"risk_indicator": "RI-01"}` with
       `result="success"` — the action succeeded and is legal, the indicator
       says a human should look; a reject raises no indicator, because
       nothing became PAID;
    9. notify the MAKER on `payment.manual_confirmed` (a confirm only) —
       not the applicant, whom `confirm_payment` has already told on
       `payment.confirmed`; see `_confirm_and_pay` for why, and for why a
       rejection notifies nobody.

    Steps 3-9 are ONE transaction: `get_db`'s commit-on-success (decision
    #37) makes the money, the ledger, the trail and the RI durable together
    or not at all. Nothing here uses decision #40's early-commit pattern —
    that is for DENIALS, and the only denials here (`ERR-ACL-001`,
    `ERR-PAY-004`) happen before any write and have nothing to preserve.
    """
    stripped = (reason or "").strip()
    if not approve and not stripped:
        raise err("ERR-VAL-001", details={"reason": "reason_required"})

    confirmation = await repo.get_manual_confirmation_for_update(db, confirmation_id)
    if confirmation is None:
        raise err("ERR-SYS-003", details={"manual_payment_confirmation": str(confirmation_id)})
    if confirmation.status != _STATUS_PENDING_CHECK:
        raise err("ERR-PAY-004", details={"reason": "already_checked"})
    if confirmation.maker_id == actor.id:
        raise err("ERR-ACL-001", details={"reason": "maker_cannot_check"})

    if approve:
        await _confirm_and_pay(db, confirmation, actor=actor)
    else:
        confirmation.reason = stripped

    # All THREE together, and only after `_confirm_and_pay` has run its own
    # flushes: `confirmed_needs_checker` is a database CHECK, so a row that
    # is `confirmed` with a NULL `checker_id` cannot exist even for the
    # length of one intermediate flush. Setting the status inside
    # `_confirm_and_pay` (where it reads more naturally) made
    # `confirm_payment`'s own flush abort the whole confirmation with a
    # CheckViolationError — the constraint doing exactly its job.
    confirmation.status = _STATUS_CONFIRMED if approve else _STATUS_REJECTED
    confirmation.checker_id = actor.id
    confirmation.checked_at = datetime.now(UTC)
    await db.flush()

    if not approve:
        await audit.log(
            db,
            action=MANUAL_REJECT_ACTION,
            user_id=actor.id,
            object_type="manual_payment_confirmation",
            object_id=confirmation.id,
            old_value={"status": _STATUS_PENDING_CHECK},
            new_value={"status": _STATUS_REJECTED},
            basis=stripped,
        )
    return confirmation


async def _confirm_and_pay(
    db: AsyncSession, confirmation: ManualPaymentConfirmation, *, actor: Any
) -> None:
    """Steps 3-6, 8 and 9 of `check_manual_confirmation`'s own order — the
    half that moves real money, kept in one place so the sequence is read as
    a whole. Step 7 is deliberately NOT here: see the caller.

    The invoice lock comes FIRST and the application lock second, through
    `confirm_payment` -> `applications.service.set_status` (`jobs.py`'s "Lock
    order"). The re-check under that lock is not decoration: a Payme
    `PerformTransaction` may have paid this invoice in the days between the
    filing and this decision, and confirming again would write a second pair
    of `allocations` rows for money that arrived once.
    """
    invoice = await repo.get_invoice_for_update(db, confirmation.invoice_id)
    if invoice is None:
        raise err("ERR-SYS-003", details={"invoice": str(confirmation.invoice_id)})
    if invoice.status != _INVOICE_PAYABLE:
        raise err("ERR-PAY-004", details={"status": invoice.status})

    transaction = ProviderTransaction(
        invoice_id=invoice.id,
        provider=MANUAL_PROVIDER,
        # Unique by construction — see `check_manual_confirmation` step 5.
        external_id=str(confirmation.id),
        amount=confirmation.amount,
        state=MANUAL_TRANSACTION_STATE,
        performed_at=confirmation.paid_at,
        # Everything a later reader needs to explain this row without
        # joining back: the document that justified it, and BOTH pairs of
        # eyes. `provider_transactions` is where a reconciliation or an
        # audit starts, and a manual row that does not name its own basis is
        # indistinguishable from a provider's.
        payload={
            "manual_confirmation_id": str(confirmation.id),
            "bank_doc_file_id": str(confirmation.bank_doc_file_id),
            "maker_id": str(confirmation.maker_id),
            "checker_id": str(actor.id),
        },
    )
    await repo.add_provider_transaction(db, transaction)

    # UNCHANGED, not widened, not copied (ruling 14): the same function the
    # Payme webhook calls. It performs no checks of its own beyond resolving
    # the recipient account — every check above is this caller's.
    await payments_service.confirm_payment(db, invoice=invoice, transaction=transaction)

    await audit.log(
        db,
        action=MANUAL_CONFIRM_ACTION,
        user_id=actor.id,
        object_type="invoice",
        object_id=invoice.id,
        old_value={"status": _INVOICE_PAYABLE},
        new_value={
            "status": invoice.status,
            "manual_confirmation_id": str(confirmation.id),
            "transaction_id": str(transaction.id),
            "amount": str(confirmation.amount),
            "maker_id": str(confirmation.maker_id),
        },
        basis=f"manual payment confirmation {confirmation.id}",
        # `result` stays "success" — see RISK_INDICATOR_MANUAL_PAID.
        extra={"risk_indicator": RISK_INDICATOR_MANUAL_PAID},
    )

    # THE MAKER, not the applicant (coordinator ruling, 2026-09-03).
    # `confirm_payment` above has already sent the applicant
    # `payment.confirmed` on both `inapp` and `sms`; adding
    # `payment.manual_confirmed` to the same person made it two billed
    # Cyrillic SMS for one payment, and the applicant does not care by which
    # door their payment was confirmed. The accountant who filed the
    # confirmation does, and nothing else tells them it was approved — so
    # the seeded template keeps a real reader instead of becoming a dead
    # seed.
    #
    # The REJECT path deliberately sends nothing: there is no
    # `payment.manual_rejected` template seeded (migration 0022 seeds only
    # `refund.decided` and `payment.manual_confirmed`), and `notify` on an
    # unseeded code silently finds no template — a call that looks like a
    # notification and delivers none. The rejection reason reaches the maker
    # through the register and the audit trail; a template for it is a
    # migration, and this stage takes no new number.
    application = await applications_service.get(db, invoice.application_id)
    if application is not None:
        await notifications_service.notify(
            db,
            event_code=payment_events.PAYMENT_MANUAL_CONFIRMED,
            recipient_user_id=confirmation.maker_id,
            params={
                "application_number": application.number or str(application.id),
                "amount": confirmation.amount,
            },
            object_type="invoice",
            object_id=invoice.id,
        )
