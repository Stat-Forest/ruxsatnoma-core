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

Task 9 adds the refunds half at the bottom of this file: `request_refund`,
`submit_refund_decision`, `approve_refund`, `list_refunds` — a manual
process (`tz/08`, decision #12) that DOES write `allocations`, the one
exception to the paragraph above, and only on `approve_refund`, never on
`request_refund`/`submit_refund_decision` (see that function's own
docstring for why the negative entries land there and not earlier).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import MediaFile
from app.core.time import add_working_days, business_today
from app.modules.admin import repo as admin_repo
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.norms import service as norms_service
from app.modules.notifications import service as notifications_service
from app.modules.payments import events as payment_events
from app.modules.payments import refunds, repo
from app.modules.payments import service as payments_service
from app.modules.payments.models import (
    ALLOCATION_ENTRY_TYPES,
    ALLOCATION_TARGETS,
    INVOICE_STATUSES,
    MANUAL_CONFIRMATION_STATUSES,
    PAYMENT_PROVIDERS,
    RECONCILIATION_RESULTS,
    RECONCILIATION_STATUSES,
    REFUND_STATUSES,
    Allocation,
    Invoice,
    ManualPaymentConfirmation,
    ProviderTransaction,
    Reconciliation,
    Refund,
)
from app.modules.payments.permissions import PAYMENTS_MANAGE

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


# --- Task 9: refunds -----------------------------------------------------
#
# `tz/08`, decision #12: a refund is a MANUAL process. The money moves
# outside this system — there is no banking refund integration, and this
# stage does not build one. What lives here is the paper trail: the
# grounds, the formula's HINT, the accountant's actual figure and its
# breakdown by source, the 20-working-day control deadline, and the
# negative `allocations` entries that make the ledger agree with what
# actually went back.
#
# **The negative entries are written on APPROVE, not on submit-decision**
# (ruling 4). `submit_refund_decision` (accountant, `payments.manage`)
# only STORES the accountant's figures and moves `requested` -> `in_review`;
# `approve_refund` (rahbar, `payments.confirm`) is the one function in this
# module that touches `allocations` for a refund, and only when the
# resolution is `returned`.
#
# **A refund never moves the invoice or the application** (ruling 6,
# `tz/05`, `design/02`: "the invoice status is not rewritten, the history
# stays intact"). There is no transition for it in either state machine,
# and this module invents none — `request_refund`/`submit_refund_decision`/
# `approve_refund` read `applications.service.get` for the OWNERSHIP check
# and the recipient-account lookup only, never `set_status`.

REFUND_REQUEST_ACTION = "refund.request"
REFUND_SUBMIT_DECISION_ACTION = "refund.submit_decision"
REFUND_APPROVE_ACTION = "refund.approve"

_STATUS_REQUESTED, _STATUS_IN_REVIEW, _STATUS_RETURNED, _STATUS_REJECTED = REFUND_STATUSES
_TARGET_RECIPIENT, _TARGET_BUDGET, _TARGET_OTHER = ALLOCATION_TARGETS
# Unpacked rather than retyped (module docstring's own vocabulary rule,
# already followed above for `RECONCILIATION_STATUSES`/`MANUAL_CONFIRMATION_
# STATUSES`): `ALLOCATION_ENTRY_TYPES[1]` is `"refund"`, the third value
# this module's own writes use alongside `service.py`'s `"payment"`/
# `"correction"`.
_, ALLOCATION_ENTRY_REFUND, _ = ALLOCATION_ENTRY_TYPES

# The refund resolution `approve_refund` accepts, spelled once. Not derived
# from `REFUND_STATUSES` (unlike the four constants above): the wire value
# is a VERB two of those four statuses share the name of, and deriving it
# from the tuple's own order would silently break the day `REFUND_STATUSES`
# is ever reordered.
REFUND_RESOLUTIONS = (_STATUS_RETURNED, _STATUS_REJECTED)


async def _may_request_refund_for(db: AsyncSession, applicant_id: uuid.UUID, *, actor: Any) -> bool:
    """`payments.manage` (the accountant) files for anyone; otherwise the
    actor must own the SAME applicant identity the application belongs to,
    or hold an effective representation of it — the exact ownership rule
    `payments.service._may_act_on_invoices_of` gives `GET /invoices/{id}`,
    reimplemented here rather than imported (that function is `service.py`'s
    own file-private helper, not part of this module's cross-file surface;
    see the lesson on module-private names)."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    if PAYMENTS_MANAGE in await auth_repo.permission_codes(db, actor):
        return True
    own_applicant = await auth_service.get_own_applicant(db, actor.id)
    if own_applicant is not None and own_applicant.id == applicant_id:
        return True
    return await auth_service.has_effective_representation_of(
        db, user_id=actor.id, applicant_id=applicant_id
    )


async def _assert_refund_basis_active(db: AsyncSession, basis_item_id: uuid.UUID) -> None:
    """An EXISTENCE check, not a validity check (the same split
    `_assert_doc_active` draws above): confirms `basis_item_id` names a
    `classifier_items` row that is not archived, nothing about whether it
    actually explains this refund."""
    item = await admin_repo.get_classifier_item(db, basis_item_id)
    if item is None or item.status != "active":
        raise err("ERR-VAL-001", details={"reason": "basis_item_not_active"})


async def _hint_for_invoice(db: AsyncSession, invoice: Invoice) -> refunds.RefundHint:
    """`request_refund`'s own chain (ruling 1): `invoice.calculation_id` ->
    `norms.service.get_calculation` -> `input_snapshot["request"]
    ["period_from"/"period_to"]` -> `refunds.hint`. **Never
    `applications.service.current_calculation`**, which answers the NEWEST
    calculation — the cross-module divergence PR #31 caught printing
    9 999 999,00 on a permit against 2 060 000,00 paid. This reads the
    calculation the INVOICE itself froze, and nothing else.

    A hint is never an error (ruling 2) — every branch below returns a
    `RefundHint` instead of raising, and `request_refund` files the refund
    regardless of which one comes back:

    - `invoice.status != "paid"` — covers BOTH "no in-force invoice" (the
      newest invoice for this application is `cancelled`/`expired`) and an
      in-force-but-still-`pending` one: neither has a PAID amount to price
      a refund's unused share against, so both read the same reason;
    - `invoice.calculation_id IS NULL`;
    - `input_snapshot` has no `"request"` key, or it is not an object;
    - `period_from`/`period_to` are missing or fail `date.fromisoformat`;
    - the parsed period is reversed or zero-length (`period_to < period_from`)
      — a malformed snapshot, never fed to `refunds.hint`, whose own
      precondition is a positive-length period.

    Everything else — including a period that has already run its course,
    which prices at a real `0.00` — goes to `refunds.hint` itself."""
    if invoice.status != "paid":
        return refunds.RefundHint(None, "no_in_force_invoice")
    if invoice.calculation_id is None:
        return refunds.RefundHint(None, "calculation_missing")
    calculation = await norms_service.get_calculation(db, invoice.calculation_id)
    snapshot = calculation.input_snapshot
    request = snapshot.get("request") if isinstance(snapshot, dict) else None
    if not isinstance(request, dict):
        return refunds.RefundHint(None, "snapshot_missing_request")
    raw_from = request.get("period_from")
    raw_to = request.get("period_to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        return refunds.RefundHint(None, "period_unparseable")
    try:
        period_from = date.fromisoformat(raw_from)
        period_to = date.fromisoformat(raw_to)
    except ValueError:
        return refunds.RefundHint(None, "period_unparseable")
    if period_to < period_from:
        return refunds.RefundHint(None, "period_zero_length")
    amount = refunds.hint(
        paid=invoice.amount, period_from=period_from, period_to=period_to, on_date=business_today()
    )
    return refunds.RefundHint(amount, None)


async def request_refund(
    db: AsyncSession,
    *,
    application_id: uuid.UUID,
    basis_item_id: uuid.UUID,
    comment: str | None,
    actor: Any,
) -> Refund:
    """`POST /refunds` — an applicant appealing their OWN application, or an
    accountant (`payments.manage`) filing on anyone's behalf (ruling 7).

    `application_id` resolves to `payments.service.invoice_for_application`'s
    in-force invoice first; when there is none (the newest invoice, if any,
    is `cancelled`/`expired`), the most recent invoice of ANY status is used
    instead — `refunds.invoice_id` is a NOT NULL FK, so a refund still needs
    an invoice to point at even when there is nothing to price a hint
    against. Only when the application has NO invoice at all (never
    approved, or approved but never invoiced) is this a hard failure
    (`ERR-SYS-003`) rather than a degenerate hint — there is nothing to
    create the row against.

    `due_at` is `add_working_days(business_today(), 20)` — `tz/08`'s
    20-working-day control deadline (RI-07)."""
    application = await applications_service.get(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if not await _may_request_refund_for(db, application.applicant_id, actor=actor):
        # Same oracle reasoning as `get_invoice_for_actor`: a stranger gets
        # the same 404 a missing application would, never a 403 that would
        # confirm the id is real.
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    await _assert_refund_basis_active(db, basis_item_id)

    invoice = await payments_service.invoice_for_application(db, application_id)
    if invoice is None:
        candidates, _total = await repo.list_invoices_by_application(
            db, application_id, limit=1, offset=0
        )
        invoice = candidates[0] if candidates else None
    if invoice is None:
        raise err(
            "ERR-SYS-003", details={"reason": "no_invoice", "application": str(application_id)}
        )

    hint = await _hint_for_invoice(db, invoice)

    row = Refund(
        application_id=application_id,
        invoice_id=invoice.id,
        basis_item_id=basis_item_id,
        suggested_amount=hint.amount,
        suggestion_reason=hint.reason,
        status=_STATUS_REQUESTED,
        requested_by=actor.id,
        requested_at=datetime.now(UTC),
        due_at=add_working_days(business_today(), 20),
        comment=comment,
    )
    await repo.add_refund(db, row)
    await audit.log(
        db,
        action=REFUND_REQUEST_ACTION,
        user_id=actor.id,
        object_type="refund",
        object_id=row.id,
        new_value={
            "application_id": str(application_id),
            "invoice_id": str(invoice.id),
            "basis_item_id": str(basis_item_id),
            "suggested_amount": str(hint.amount) if hint.amount is not None else None,
            "suggestion_reason": hint.reason,
        },
    )
    return row


async def submit_refund_decision(
    db: AsyncSession,
    refund_id: uuid.UUID,
    *,
    final_amount: Decimal,
    budget_amount: Decimal,
    recipient_amount: Decimal,
    other_amount: Decimal,
    comment: str | None,
    actor: Any,
) -> Refund:
    """`POST /refunds/{id}/submit-decision` — the accountant's own half
    (ruling 4): STORE the figures, move `requested` -> `in_review`, touch no
    money. `approve_refund` is what writes the ledger.

    The breakdown is checked against `refunds.breakdown_is_complete` BEFORE
    the row is written — `ERR-VAL-001`, not an IntegrityError 500 — even
    though `returned_needs_complete_breakdown` itself only fires once
    `approve_refund` moves the row to `returned`, not at this status: a
    wrong number caught here never reaches the rahbar's screen at all."""
    row = await repo.get_refund_for_update(db, refund_id)
    if row is None:
        raise err("ERR-SYS-003", details={"refund": str(refund_id)})
    if row.status != _STATUS_REQUESTED:
        raise err("ERR-PAY-006", details={"status": row.status})
    if not refunds.breakdown_is_complete(
        final_amount, budget_amount, recipient_amount, other_amount
    ):
        raise err("ERR-VAL-001", details={"reason": "breakdown_incomplete"})

    old_value = {"status": row.status}
    row.final_amount = final_amount
    row.budget_amount = budget_amount
    row.recipient_amount = recipient_amount
    row.other_amount = other_amount
    row.status = _STATUS_IN_REVIEW
    if comment:
        row.comment = comment
    await db.flush()
    await audit.log(
        db,
        action=REFUND_SUBMIT_DECISION_ACTION,
        user_id=actor.id,
        object_type="refund",
        object_id=row.id,
        old_value=old_value,
        new_value={
            "status": row.status,
            "final_amount": str(final_amount),
            "budget_amount": str(budget_amount),
            "recipient_amount": str(recipient_amount),
            "other_amount": str(other_amount),
        },
    )
    return row


class ApprovedRefund(NamedTuple):
    """What `approve_refund` answers with — the refund row, and the
    `allocations` it just wrote (empty on `rejected`, since nothing moved).
    The router cannot compute the second half itself (router -> service ->
    repo), the same reason `FiledConfirmation` above carries
    `amount_matches_invoice` alongside its own row."""

    refund: Refund
    allocations: list[Allocation]


async def approve_refund(
    db: AsyncSession,
    refund_id: uuid.UUID,
    *,
    resolution: str,
    comment: str | None,
    actor: Any,
) -> ApprovedRefund:
    """`POST /refunds/{id}/approve` — the rahbar's (`payments.confirm`) own
    half, and the only place a refund ever touches `allocations` (ruling 4).

    `resolution="returned"`:

    1. re-validates `refunds.breakdown_is_complete` against the row's OWN
       stored figures — defensive, not decorative: `submit_refund_decision`
       already checked the same arithmetic, but re-checking here means a
       future write path that reaches `in_review` some other way cannot
       skip straight past this module's one arithmetic guard into the
       database CHECK;
    2. resolves the recipient's account the SAME way `service.confirm_payment`
       does (`payments_service.resolve_recipient_account`) — the leshoz's
       own bank account, `None` when it has none on file. The budget half's
       account is `None` unconditionally (ruling 5, `tz/12` #15 — the state
       budget's account number is stored nowhere in this system) and so is
       the `other` bucket's, which names no account of its own either;
    3. writes ONE negative `entry_type="refund"` allocation per NON-ZERO
       component, each carrying `refund_id` — a `0.00` component writes no
       row, the same "nothing from this source" reading
       `refunds.breakdown_is_complete` already gives it;
    4. moves the refund to `returned`.

    `resolution="rejected"` writes NOTHING to `allocations` — no money ever
    moved, so there is nothing to reverse — and moves the refund straight to
    `rejected`.

    **Neither branch touches the invoice or the application** (ruling 6):
    `design/02` — "the invoice status is not rewritten, the history stays
    intact" — and `tz/05` gives neither state machine a transition for a
    refund at all.

    Notifies the applicant on `refund.decided` either way (migration `0022`
    seeds it for `inapp`/`sms`) — an accountant filing on someone else's
    behalf does not change who the money (or its absence) belongs to."""
    row = await repo.get_refund_for_update(db, refund_id)
    if row is None:
        raise err("ERR-SYS-003", details={"refund": str(refund_id)})
    if row.status != _STATUS_IN_REVIEW:
        raise err("ERR-PAY-006", details={"status": row.status})

    old_value = {"status": row.status}
    written: list[Allocation] = []

    if resolution == _STATUS_RETURNED:
        if row.final_amount is None or not refunds.breakdown_is_complete(
            row.final_amount, row.budget_amount, row.recipient_amount, row.other_amount
        ):
            raise err("ERR-VAL-001", details={"reason": "breakdown_incomplete"})

        application = await applications_service.get(db, row.application_id)
        recipient_account = await payments_service.resolve_recipient_account(
            db,
            contour_id=application.contour_id if application else None,
            assigned_org_id=application.assigned_org_id if application else None,
        )
        components = (
            (_TARGET_RECIPIENT, recipient_account, row.recipient_amount),
            (_TARGET_BUDGET, None, row.budget_amount),
            (_TARGET_OTHER, None, row.other_amount),
        )
        for target, account, amount in components:
            if not amount:
                continue
            written.append(
                Allocation(
                    invoice_id=row.invoice_id,
                    refund_id=row.id,
                    entry_type=ALLOCATION_ENTRY_REFUND,
                    target=target,
                    account=account,
                    amount=-amount,
                    note=f"refund {row.id} approved ({resolution})",
                )
            )
        if written:
            await repo.add_allocations(db, written)
        row.status = _STATUS_RETURNED
    else:
        row.status = _STATUS_REJECTED

    row.decided_by = actor.id
    row.decided_at = datetime.now(UTC)
    if comment:
        row.comment = comment
    await db.flush()

    await audit.log(
        db,
        action=REFUND_APPROVE_ACTION,
        user_id=actor.id,
        object_type="refund",
        object_id=row.id,
        old_value=old_value,
        new_value={
            "status": row.status,
            "allocations": [str(a.id) for a in written],
        },
        basis=comment,
    )

    application = await applications_service.get(db, row.application_id)
    if application is not None:
        await notifications_service.notify(
            db,
            event_code=payment_events.REFUND_DECIDED,
            recipient_user_id=application.submitted_by_user_id,
            params={
                "application_number": application.number or str(application.id),
                "status": row.status,
                "amount": row.final_amount if row.final_amount is not None else Decimal("0.00"),
            },
            object_type="refund",
            object_id=row.id,
        )

    return ApprovedRefund(refund=row, allocations=written)


async def list_refunds(
    db: AsyncSession,
    *,
    application_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
    actor: Any,
) -> tuple[list[Refund], int]:
    """`GET /refunds` — `payments.view` sees every refund (optionally
    narrowed by `application_id`/`status`); `actor` is accepted for symmetry
    with `list_reconciliations` and is not used to filter further — the
    route's own `PAYMENTS_VIEW` gate already decides who may call this."""
    return await repo.list_refunds(
        db, application_id=application_id, status=status, limit=limit, offset=offset
    )
