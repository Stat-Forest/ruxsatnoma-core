"""Payments service — turning an approved application into an invoice, and
answering who may read it (design/02 § payments, plan `03.10a-payments-core`
task 2).

Public surface for levels 4+ (permits 3.11): `invoice_for_application()`,
`is_paid()`, `allocations_for()` — frozen by Task 7, see the dedicated
comment right before their definitions for what a caller may and may not do
with them.

Public surface for the event bus (`subscribers.py`, registered in
`app/event_subscriptions.py`):

- `issue_invoice(db, application_id) -> Invoice` — the whole business action
  behind `applications.events.APPLICATION_APPROVED`: freeze the application's
  current calculation into an invoice, move the application to INVOICED, and
  notify the applicant. Idempotent (see its own docstring). Checks
  `invoice_for_application` first.
- `cancel_invoice_for_application(db, application_id) -> Invoice | None` — the
  business action behind `applications.events.APPLICATION_CANCELLED`.
  Idempotent and silent when there is no in-force invoice, and it refuses
  (loudly logged, `None` returned) to cancel one that is already `paid` —
  see its own docstring.
- `confirm_payment(db, *, invoice, transaction) -> None` (task 4, ruling J) —
  the whole business action behind a successful Payme `PerformTransaction`:
  invoice -> paid, the 50/50 ledger written, application -> PAID, the
  applicant notified, `payment_confirmed` published on the bus. Called from
  `payme.py` ONLY, already inside the caller's own transaction and already
  past every Payme-protocol check (idempotency, amount, payability) — this
  function performs no check of its own beyond resolving the recipient
  account.
- `record_reversal(db, *, invoice, transaction, reason) -> None` (3.10b task
  8, ruling 15) — the mirror of `confirm_payment` for money that came BACK:
  Payme cancelled an already-performed transaction. It RECORDS the reversal
  (negating `correction` entries, an open `reconciliations` row, RI-01 and,
  when a permit exists, RI-10) and deliberately moves neither the invoice nor
  the application. Called from `payme.py`'s state-`2` branch ONLY. See the
  KNOWN GAP paragraph in the public-surface comment below for the whole shape
  of what shipped and what stays open.

`get_invoice_for_actor`/`list_invoices_for_actor`/`create_pay_intent` are
this module's OWN router-facing functions (they take an HTTP `actor: User`,
unlike the functions above) — not part of the cross-module public surface.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import numbers
from app.core.errors import err
from app.core.events import Event, publish
from app.core.time import TASHKENT, business_today
from app.modules.admin import repo as admin_repo
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.integrations.adapters import payme as payme_adapter
from app.modules.notifications import service as notifications_service
from app.modules.payments import events, ledger, repo
from app.modules.payments.models import (
    RECONCILIATION_RESULTS,
    RECONCILIATION_STATUSES,
    Allocation,
    Invoice,
    PaymentIntent,
    ProviderTransaction,
    Reconciliation,
)
from app.modules.payments.permissions import PAYMENTS_VIEW

logger = structlog.get_logger(__name__)

# CLAUDE.md's audit invariant: action codes are "<object>.<verb>" in English,
# and the constant lives with the acting module (mirrors
# applications.service.APPLICATION_STATUS_CHANGE's own idiom).
INVOICE_ISSUE = "invoice.issue"
INVOICE_CANCEL = "invoice.cancel"
INVOICE_PAY = "invoice.pay"
PAY_INTENT_CREATE = "payment_intent.create"
# 3.10b task 8: money that was already confirmed going back (`record_reversal`).
# "record", not "reverse": the money moved outside the system and this action
# only writes down that it did — nothing here undoes a payment.
REVERSAL_RECORD = "payment.reversal_record"

# `tz/10`'s two indicators this module can raise, spelled once each (ruling 2:
# an RI is an `audit.service.log` row with `extra={"risk_indicator": ...}` —
# there is no `risk_indicators` table and 4.2 `oversight` harvests these rows).
#
# RI-01 «PAID без подтверждения провайдера/банка» — raised by BOTH doors that
# can produce one: the maker-checker manual PAID (`backoffice_service`, which
# imports this constant rather than keeping a second literal) and a post-perform
# reversal, where the provider withdraws a confirmation the invoice still bears.
# RI-10 «Разрешение активировано без оплаты» — 3.11a raises it on a REFUSED
# issuance; here it is a permit that already exists over money that came back.
RISK_INDICATOR_UNCONFIRMED_PAID = "RI-01"
RISK_INDICATOR_PERMIT_WITHOUT_PAYMENT = "RI-10"

# design/03 §"Public numbers": every invoice number starts with this prefix.
INVOICE_NUMBER_PREFIX = "INV"

# design/02: an invoice is payable for 10 calendar days from issuance.
DUE_PERIOD = timedelta(days=10)

# Derived from the model's own tuples — what `result_valid`/`status_valid` are
# built from — rather than retyped, the idiom `backoffice_service`'s own
# `_INVOICE_PAYABLE` uses: a reordering goes red in the tests rather than
# silently changing which row the register shows.
RECONCILIATION_RESULT_DISCREPANCY = RECONCILIATION_RESULTS[1]
RECONCILIATION_STATUS_OPEN = RECONCILIATION_STATUSES[0]


# --- Task 7: the public surface for 3.11 permits ---------------------------
#
# Three entry points, and nothing else a level-4+ caller may use to learn
# about an invoice or its ledger. The contract below is FROZEN once this
# task lands — 3.11 builds against it starting now, in a parallel worktree.
#
# - `invoice_for_application(db, application_id) -> Invoice | None` — the
#   in-force (`pending`/`paid`) invoice, or `None` when the application was
#   never invoiced, or its invoice was cancelled/expired with no new one
#   issued since. Existed since Task 2 (`issue_invoice`'s own idempotency
#   check reads it first); this task only freezes its signature.
# - `is_paid(db, application_id) -> bool` — "did the provider confirm the
#   money for this application". `True` only once the in-force invoice's own
#   `status == "paid"`; `False` for no invoice at all, a `pending` one, or
#   one that expired or was cancelled — a caller weighing up whether an
#   application is settled does not need those distinguished further. A thin
#   wrapper over `invoice_for_application`, so the two can never disagree
#   about what "in force" means.
#
#   **NOTHING OUTSIDE THIS MODULE CALLS IT.** This bullet said "3.11 calls
#   exactly this before building a permit" until 2026-09-03, and 3.11 does
#   not: `permits.service.issue` reads the APPLICATION's own status
#   (`!= "PAID"` -> `ERR-PAY-001` and an RI-10 audit row), because `permits`
#   may not read `payments` at all — both are level 4 (`design/01` rule 3).
#   `applications.status` reaching PAID is this module's own write, through
#   `applications.service.set_status` in `confirm_payment`, so the two agree
#   by construction rather than by anyone checking. Kept on the surface for
#   the callers that legitimately want the invoice-side answer (3.10b's
#   refunds, 4.3's reports), not as a guard anybody depends on today.
#
#   **THE KNOWN GAP 3.10a NAMED HERE, and what 3.10b actually did with it.**
#   Kept rather than deleted: a reader needs the history more than the tidy
#   version. 3.10a wrote that Payme's `CancelTransaction` on an
#   ALREADY-PERFORMED transaction (state `2` -> `-2`; `design/04` §3.5 reason
#   `5` is literally "funds returned") recorded the reversal on
#   `provider_transactions` and NOTHING else — the invoice still `paid`, the
#   application still `PAID`, `allocations` still carrying two `payment` rows
#   summing to money that had gone back, this function still answering `True`,
#   and, because issuance gates on the application's status, a permit issuable
#   for a refunded payment. It expected 3.10b to move the application off PAID.
#
#   **3.10b did not, and could not** (ruling 15).
#   `applications.service.APPLICATION_TRANSITIONS["PAID"]` is
#   `frozenset({"PERMIT_ISSUED"})`, and `tz/05` gives PAID no other exit.
#   Adding one is a change to the application state machine — level 3, owned
#   by stage 3.9, read by 3.11's issuance gate and by 3.11b's revoke — so
#   making it from a `payments` branch would have shipped a silent
#   cross-module break in place of a fix. `design/02` says the same of the
#   invoice: its status is not rewritten, the history stays intact.
#
#   **What DID ship, in `record_reversal` (below), called from
#   `payme._cancel_transaction`'s state-`2` branch:** the ledger stays
#   arithmetically true (one `correction` entry per `payment` row, so the
#   invoice's whole ledger sums to `0.00` instead of claiming money that is
#   gone); the discrepancy register carries an OPEN row whose `difference` is
#   the reversed amount — the operator's handle, and a case only a human can
#   finish; RI-01 is raised («PAID без подтверждения провайдера/банка» — the
#   provider withdrew a confirmation the invoice still bears); and RI-10 too
#   («Разрешение активировано без оплаты») when a permit already exists for
#   that application.
#
#   **What is STILL OPEN, and is not a defect in the code below.** The invoice
#   stays `paid` and the application stays `PAID`, so this function still
#   answers `True` after a reversal, and between the reversal and an operator
#   acting on the register row `permits.service.issue` will still issue
#   against that application. The remedy for an already-issued permit is
#   3.11b's revoke; the question of what SHOULD happen to a permit whose
#   payment was reversed is filed for the Agency in `tz/12`, next to #16.
#   Do not paper over any of it by widening `is_paid`: the answer is a
#   `tz/05` transition somebody must decide, not a read that lies differently.
# - `allocations_for(db, invoice_id) -> list[Allocation]` — the whole
#   ledger for one invoice, oldest first — what 4.3's reports read. Every
#   row this module ever wrote for this invoice, `entry_type` unfiltered:
#   `"payment"` (two rows, recipient + budget) from `confirm_payment`,
#   `"correction"` from `record_reversal` and `"refund"` from 3.10b's refund
#   register — a caller must not assume every row it gets back is a payment,
#   and must not assume they are all positive.
#
# No permission or zone rule on any of the three — the caller is another
# SERVICE inside this process, not an HTTP actor, mirroring
# `applications.service.get`/`norms.service.effective_norm`.
#
# A level-4+ caller must NEVER:
#   - read `invoices` or `allocations` as tables of its own — no
#     `payments.repo` import, no `select(Invoice)`/`select(Allocation)`
#     against this module's tables from outside it. Every fact reachable
#     that way is already one of the three functions above (module
#     boundary, CLAUDE.md — the same reasoning `norms.service`'s own
#     public-surface comment states for `tariffs`/`rule_parameters`).
#   - set an invoice's `status` directly. `issue_invoice`,
#     `cancel_invoice_for_application` and `confirm_payment` (below) are
#     the only writers, each already wired to the one event that should
#     trigger it (`applications.events`' APPROVED/CANCELLED, and a
#     confirmed Payme `PerformTransaction`) — a level-4+ module has no
#     business of its own moving an invoice between `pending`/`paid`/
#     `expired`/`cancelled`.
# -----------------------------------------------------------------------------


async def invoice_for_application(db: AsyncSession, application_id: uuid.UUID) -> Invoice | None:
    """The in-force (`pending`/`paid`) invoice for `application_id`, or
    `None`. No permission or zone rule: the caller is another SERVICE inside
    this process, same shape as `applications.service.get`."""
    return await repo.get_in_force_invoice(db, application_id)


async def is_paid(db: AsyncSession, application_id: uuid.UUID) -> bool:
    """`True` once the in-force invoice for `application_id` is `paid`;
    `False` for no invoice, a `pending` one, or one that expired or was
    cancelled.

    Called by NOTHING outside this module. This docstring called itself "the
    guard 3.11 calls before issuing a permit" until 2026-09-03; `permits`
    may not read `payments` (`design/01` rule 3 — both level 4) and
    `permits.service.issue` checks the application's own status instead.
    See the banner above for what that means for the refund gap."""
    invoice = await invoice_for_application(db, application_id)
    return invoice is not None and invoice.status == "paid"


async def allocations_for(db: AsyncSession, invoice_id: uuid.UUID) -> list[Allocation]:
    """The whole ledger for one invoice, oldest first. See the banner above
    — no permission or zone rule, the caller is another service."""
    return list(await repo.list_allocations_by_invoice(db, invoice_id))


async def issue_invoice(db: AsyncSession, application_id: uuid.UUID) -> Invoice:
    """The whole business action behind `APPLICATION_APPROVED`: freeze the
    application's current calculation into an invoice, move the application to
    INVOICED, and notify the applicant — all inside the caller's own
    transaction (the event bus runs handlers synchronously).

    Idempotent BY CONSTRUCTION, not by catching a constraint: looks for an
    EXISTING in-force invoice first and returns it unchanged, before this ever
    touches `set_status`. The event fires inside the approval's own
    transaction, so a retry (a retried request, or this test file's own
    repeated-publish test) re-fires it, and a second
    `set_status(INVOICED -> INVOICED)` is illegal
    (`applications.APPLICATION_TRANSITIONS` has no self-loop) — so this early
    return is load-bearing, not decoration. `uq_invoices_one_in_force`
    (migration 0017) is the backstop for a genuine concurrent race between two
    DIFFERENT transactions, never the primary mechanism: relying on catching
    its `IntegrityError` here would turn a harmless same-transaction retry
    into a rolled-back approval.

    Never recomputes the amount — reads it once from
    `applications.service.current_calculation`, the frozen price 3.7's own
    preview/save split exists to protect.
    """
    existing = await invoice_for_application(db, application_id)
    if existing is not None:
        return existing

    calculation = await applications_service.current_calculation(db, application_id)
    if calculation is None:
        # `ERR-VAL-001` (422), NOT `ERR-SYS-003` (404 «Ресурс не найден»), which
        # is what this raised until 2026-09-03. This function has no HTTP route
        # of its own: it runs as a bus subscriber INSIDE the publisher's
        # transaction, so 3.9's `POST /applications/{id}/approve` is what
        # answers — and it answered 404, indistinguishable from "no such
        # application", for a request whose application very much exists and
        # whose id was perfectly good. The approval rolls back either way (the
        # bus is synchronous and in-transaction, by design); what the reviewer
        # gets back must at least say WHY.
        #
        # The same code and the same `reason` as `permits.service.issue`'s
        # identical refusal, so one condition — "this application has no priced
        # calculation" — has one representation on both sides of the seam. A
        # dedicated `ERR-APP-005` was considered and rejected: it would have to
        # replace permits' code too to be an improvement, and neither audit
        # asked for that.
        #
        # 3.9 should make this unreachable: an application must not be able to
        # reach APPROVED without a calculation. Until it does, this is the loud
        # failure — never a silent zero-amount invoice.
        raise err(
            "ERR-VAL-001",
            details={"reason": "no_calculation", "application": str(application_id)},
        )

    issued_at = datetime.now(UTC)
    invoice = Invoice(
        application_id=application_id,
        calculation_id=calculation.id,
        number=await numbers.next_public_number(db, INVOICE_NUMBER_PREFIX, business_today()),
        amount=calculation.amount,
        issued_at=issued_at,
        due_at=issued_at + DUE_PERIOD,
    )
    await repo.add(db, invoice)
    await audit.log(
        db,
        action=INVOICE_ISSUE,
        object_type="invoice",
        object_id=invoice.id,
        new_value={
            "application_id": str(application_id),
            "calculation_id": str(calculation.id),
            "amount": str(invoice.amount),
            "number": invoice.number,
        },
    )

    application = await applications_service.set_status(db, application_id, to_status="INVOICED")

    await notifications_service.notify(
        db,
        event_code=events.INVOICE_ISSUED,
        recipient_user_id=application.submitted_by_user_id,
        params={
            "application_number": application.number,
            "amount": invoice.amount,
            # CLAUDE.md "Time": store UTC, display Asia/Tashkent — a plain
            # `.date()` here would read the UTC calendar day, off by one for
            # roughly five hours a day (the same class business_today()
            # exists to avoid, applied to a stored value rather than "now").
            "due_date": invoice.due_at.astimezone(TASHKENT).date(),
        },
        object_type="invoice",
        object_id=invoice.id,
    )
    return invoice


async def cancel_invoice_for_application(
    db: AsyncSession, application_id: uuid.UUID
) -> Invoice | None:
    """The business action behind `APPLICATION_CANCELLED`: move the in-force
    invoice, if any, to `cancelled`. Idempotent and silent when there is
    none — a withdrawal on an application that was never invoiced, or a
    repeated event, must not raise (ruling 16, half 1).

    A PAID invoice is never cancelled (whole-branch review). "In force" is
    `pending` OR `paid` (`repo.IN_FORCE_STATUSES`), so this handler is
    handed a settled invoice as readily as an unpaid one, and cancelling
    that one destroys a confirmed payment: `is_paid` flips back to `False`,
    the two `allocations` rows are left pointing at a cancelled invoice, and
    the citizen who paid gets neither a permit nor any record of a refund
    owed. (`is_paid` is not what gates issuance — `permits.service.issue`
    reads the application's own status, which this module also writes — but
    the invoice-side books are wrong either way.)
    Reversing money is 3.10b's `refunds`, and it is not something an
    application-cancelled event may trigger by omission.

    `APPLICATION_TRANSITIONS["PAID"] == {"PERMIT_ISSUED"}` today, so nothing
    publishes `APPLICATION_CANCELLED` for a paid application — but that
    guard lives in ANOTHER module, and 3.9a-flow's `submit()` already writes
    a transition without routing through `set_status` at all, so it is not a
    guarantee this module may lean on. Refusing here is fail-safe: the
    application still cancels, and the settled invoice stays settled for
    3.10b to reverse deliberately."""
    # Lock order (this module's invariant): the invoice before the
    # application — `payme._perform_transaction`/`confirm_payment` and
    # `jobs._expire_one_invoice` (see its module docstring's "Lock order")
    # both take it in that order. This handler runs AFTER `set_status` has
    # already locked the application row (it is the `APPLICATION_CANCELLED`
    # subscriber, fired from inside that same transaction), so it must take
    # the invoice lock through the repo's locking helper, never an unlocked
    # read left to become a blind write at flush — do not reintroduce that.
    invoice = await repo.get_in_force_invoice_for_update(db, application_id)
    if invoice is None:
        return None
    if invoice.status != "pending":
        logger.error(
            "payments.cancel_invoice_refused",
            invoice_id=str(invoice.id),
            application_id=str(application_id),
            status=invoice.status,
        )
        return None

    previous_status = invoice.status
    invoice.status = "cancelled"
    await audit.log(
        db,
        action=INVOICE_CANCEL,
        object_type="invoice",
        object_id=invoice.id,
        old_value={"status": previous_status},
        new_value={"status": "cancelled"},
    )
    return invoice


async def _holds_payments_view(db: AsyncSession, actor: User) -> bool:
    """Holds `payments.view`, or is the superuser that passes every permission
    gate (decision #41 ruling 2) — the same two-branch shape
    `norms.service._holds_tariffs_publish`/`gis.service._may_manage_layers`
    use for a rule INSIDE a handler, as opposed to a `require_permission`
    dependency on the route itself (needed here because even a caller
    holding NEITHER `payments.view` NOR any grant at all must still reach
    these routes, to read their OWN invoice)."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return PAYMENTS_VIEW in await auth_repo.permission_codes(db, actor)


async def _may_act_on_invoices_of(
    db: AsyncSession, applicant_id: uuid.UUID, *, actor: User
) -> bool:
    """`payments.view` (or sys_admin) sees or pays any invoice; otherwise the
    actor must OWN the same applicant identity the invoice's application
    belongs to, OR hold an EFFECTIVE REPRESENTATION of it (task 5's
    ownership ruling) — matched on `applicant_id`, never
    `submitted_by_user_id`: a legal entity has several representatives, and
    the one who happens to have submitted THIS particular application is
    not the only one entitled to see or pay its invoice. Shared by the two
    read routes (`get_invoice_for_actor`/`list_invoices_for_actor`) and the
    pay-intent route (`create_pay_intent`) — one rule, three callers, so the
    representation gap Task 2 deliberately carried to this task is closed
    for reads too, not just for paying."""
    if await _holds_payments_view(db, actor):
        return True
    own_applicant = await auth_service.get_own_applicant(db, actor.id)
    if own_applicant is not None and own_applicant.id == applicant_id:
        return True
    return await auth_service.has_effective_representation_of(
        db, user_id=actor.id, applicant_id=applicant_id
    )


async def get_invoice_for_actor(db: AsyncSession, invoice_id: uuid.UUID, *, actor: User) -> Invoice:
    """`GET /invoices/{id}`'s authorization. Refuses with `ERR-SYS-003` (404)
    both when the invoice does not exist AND when the actor may not see it —
    never `ERR-ACL-001` (403), which would confirm to a stranger that the
    invoice exists (same reasoning as `notifications.service.mark_read`)."""
    invoice = await repo.get_invoice(db, invoice_id)
    if invoice is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if not await _may_act_on_invoices_of(db, application.applicant_id, actor=actor):
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    return invoice


async def list_invoices_for_actor(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User, limit: int, offset: int
) -> tuple[list[Invoice], int]:
    """`GET /invoices?application_id=`'s authorization — same rule as
    `get_invoice_for_actor`, applied to the application rather than one
    invoice, and the same 404-not-403 reasoning: a stranger asking about an
    application that is not theirs cannot tell it apart from one that does
    not exist at all."""
    application = await applications_service.get(db, application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    if not await _may_act_on_invoices_of(db, application.applicant_id, actor=actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    return await repo.list_invoices_by_application(db, application_id, limit=limit, offset=offset)


async def create_pay_intent(
    db: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    provider: str,
    actor: User,
    idempotency_key: uuid.UUID,
) -> tuple[PaymentIntent, str]:
    """`POST /invoices/{id}/pay-intents` — the applicant's (or an effective
    representative's) own side of starting a payment (design/03 §payments,
    design/04 §3.8). Returns the new `payment_intents` row and the
    checkout-redirect URL (`integrations.adapters.payme.build_checkout_url`
    — pure string construction, no network call, so `PAYME_MODE` never
    branches this path, ruling).

    404, never 403, both when the invoice does not exist and when the actor
    may not act on it (same reasoning as `get_invoice_for_actor`) — reuses
    `_may_act_on_invoices_of`, so the SAME ownership-or-representation rule
    that gates READING an invoice also gates PAYING it.

    A non-`pending` invoice (already `paid`, `cancelled`, or `expired`)
    refuses with `ERR-PAY-004` (409, this module's own state-conflict code
    — the sibling `ERR-GIS-005`/`ERR-NORM-005` already have): a settled
    invoice handing out a working-looking checkout link would persist a
    `payment_intents` row and an audit entry for an action that can never
    complete, even though Payme's own `CheckPerformTransaction`/
    `CreateTransaction`/`PerformTransaction` (task 4) would independently
    refuse the actual payment with `-31008` — not exploitable, but not
    correct either. Checked BEFORE `due_at`: an invoice's status is the more
    fundamental precondition (a non-pending invoice was never going to be
    payable, regardless of the clock).

    An invoice past `due_at` refuses with `ERR-PAY-002`, checked against the
    WALL CLOCK, never `invoice.status`: Task 6's expiry job (not built on
    this branch) is what eventually flips `status` to `'expired'`, so a
    `pending` invoice can already be past its own window before that job
    catches up — relying on `status` alone would leave exactly that gap
    payable. (A `status='expired'` row is caught by the check above
    instead, once Task 6 starts writing it.)

    `idempotency_key` is the HTTP `Idempotency-Key` header's own value
    (`auth.deps.idempotency_context`, the router's job to resolve) — the
    value a client repeats to avoid opening two intents for one click
    (`PaymentIntent`'s own docstring). The column carries no DB unique
    constraint by design; `app/core/idempotency.py`'s own table is what
    de-duplicates the HTTP request itself.
    """
    invoice = await repo.get_invoice(db, invoice_id)
    if invoice is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if not await _may_act_on_invoices_of(db, application.applicant_id, actor=actor):
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if invoice.status != "pending":
        raise err("ERR-PAY-004", details={"invoice": str(invoice_id), "status": invoice.status})
    if datetime.now(UTC) > invoice.due_at:
        raise err("ERR-PAY-002", details={"invoice": str(invoice_id)})

    payment_url = payme_adapter.build_checkout_url(
        invoice_number=invoice.number, amount=invoice.amount
    )
    intent = PaymentIntent(
        invoice_id=invoice.id,
        provider=provider,
        amount=invoice.amount,
        status="created",
        idempotency_key=idempotency_key,
    )
    await repo.add_payment_intent(db, intent)
    await audit.log(
        db,
        action=PAY_INTENT_CREATE,
        user_id=actor.id,
        object_type="payment_intent",
        object_id=intent.id,
        new_value={"invoice_id": str(invoice.id), "provider": provider},
    )
    return intent, payment_url


async def _resolve_recipient_account(
    db: AsyncSession, *, contour_id: uuid.UUID | None, assigned_org_id: uuid.UUID | None
) -> str | None:
    """Ruling H: the leshoz's own bank account for the 50/50 recipient half —
    `contour_id` -> `gis.service.contour_organization` ->
    `admin.repo.get_organization` -> `organization.requisites.get("account")`,
    falling back to `assigned_org_id` when there is no contour. `None`
    (never a placeholder string) whenever any step comes up empty: a leshoz
    without bank details, or without even an assigned organization, must
    never block money that has already arrived — `allocations.account` is
    nullable for exactly this (Task 3 ruling).

    Takes the two fields it actually reads, not the whole `Application`
    (review finding I3): `payments` may not import `applications.models` —
    CLAUDE.md's module boundary, and the ONLY import of it anywhere in
    `app/` outside `applications` itself and `models_registry.py` before
    this fix. `confirm_payment` (the caller) already holds the row via
    `applications.service.get`, itself the sanctioned cross-module surface —
    only the TYPE import for this helper's own signature was the violation."""
    org_id: uuid.UUID | None
    if contour_id is not None:
        org_id = await gis_service.contour_organization(db, contour_id)
    else:
        org_id = assigned_org_id
    if org_id is None:
        return None
    organization = await admin_repo.get_organization(db, org_id)
    if organization is None:
        return None
    account = organization.requisites.get("account")
    return account if isinstance(account, str) else None


async def confirm_payment(
    db: AsyncSession, *, invoice: Invoice, transaction: ProviderTransaction
) -> None:
    """The whole business action behind a confirmed payment — invoice ->
    paid, the 50/50 ledger written, application -> PAID, the applicant
    notified, `payment_confirmed` published — all inside the CALLER's
    transaction: the caller owns the session, and this function neither
    commits nor performs any check of its own beyond resolving the recipient
    account. Every check is the caller's.

    TWO callers, and they are the whole list:

    - `payme._perform_transaction` (task 4, ruling J) — a just-confirmed
      `PerformTransaction`, already past every Payme-protocol check
      (idempotency, amount, payability) this function does not repeat.
    - `backoffice_service._confirm_and_pay` (3.10b task 7) — the checker's
      half of the maker-checker manual PAID (`tz/08` §4's one exception to
      `tz/05` invariant 3), handing in a SYNTHETIC `provider="manual"`
      transaction. It has run its own checks instead: maker != checker, the
      invoice locked and re-read as `pending`, and the amount bounded
      `> 0` by `backoffice_schemas.ManualConfirmationIn`.

    Ruling I: the amount split is `transaction.amount` — the money that
    actually arrived — never `invoice.amount`. Task 3's own tests prove the
    sourcing rule this function relies on (`ledger.entries_for`).

    **The two amounts are NOT equal by construction.** They are on the Payme
    path, where `CheckPerformTransaction`/`CreateTransaction` refuse a
    mismatch with `-31001`; this docstring claimed that as a general
    invariant until 2026-09-03, and the manual door deliberately breaks it.
    An accountant may confirm an UNDERPAYMENT — money that really arrived,
    less than was owed — and 3.10b files the difference as an open
    `reconciliations` row rather than refusing it. So a caller reading this
    must not assume `transaction.amount == invoice.amount`: the ledger below
    settles what arrived, and an invoice can be `paid` with less than its own
    amount allocated.
    """
    invoice.status = "paid"
    invoice.paid_at = transaction.performed_at

    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        # invoice.application_id is a NOT NULL FK — unreachable in practice;
        # guarded rather than crashing into `None.contour_id` below. Caught
        # by payme_router.py's generic handler and answered -32400.
        raise err("ERR-SYS-003", details={"invoice": str(invoice.id)})

    recipient_account = await _resolve_recipient_account(
        db, contour_id=application.contour_id, assigned_org_id=application.assigned_org_id
    )
    entries = ledger.entries_for(
        invoice=invoice,
        transaction=transaction,
        recipient_account=recipient_account,
        # Ruling H: the state budget's account number is not in the system
        # at all in 3.10a — never a placeholder string.
        budget_account=None,
    )
    await repo.add_allocations(db, entries)

    await audit.log(
        db,
        action=INVOICE_PAY,
        object_type="invoice",
        object_id=invoice.id,
        old_value={"status": "pending"},
        new_value={
            "status": "paid",
            "transaction_id": str(transaction.id),
            "amount": str(transaction.amount),
        },
    )

    # set_status re-fetches and locks the row itself (applications.repo.
    # get_application_for_update) — the `application` read above is only
    # for the ledger's account resolution, never mutated directly here
    # (module boundary: a level-4 caller never writes applications.status
    # by hand).
    application = await applications_service.set_status(
        db, invoice.application_id, to_status="PAID"
    )

    await notifications_service.notify(
        db,
        event_code=events.PAYMENT_CONFIRMED_NOTIFICATION_CODE,
        recipient_user_id=application.submitted_by_user_id,
        params={"application_number": application.number, "amount": transaction.amount},
        object_type="invoice",
        object_id=invoice.id,
    )

    # BOTH ids, never `invoice_id` alone (whole-branch review): a subscriber
    # given only the invoice id has no way back to the application — the
    # frozen public surface takes an `application_id` in both directions
    # (`invoice_for_application`, `is_paid`) and forbids a level-4 caller
    # from reading `invoices` as a table. 3.11's own subscriber reads
    # `application_id` off this payload. Both values are IDENTIFIERS, so
    # ruling 15's "no second source of truth for money" — which is why the
    # `applications` events carry no amount and no number — is untouched.
    await publish(
        db,
        Event(
            name=events.PAYMENT_CONFIRMED,
            payload={
                "invoice_id": str(invoice.id),
                "application_id": str(invoice.application_id),
            },
        ),
    )


async def record_reversal(
    db: AsyncSession,
    *,
    invoice: Invoice,
    transaction: ProviderTransaction,
    reason: int | None,
) -> None:
    """Money that was already confirmed has gone back: Payme cancelled an
    ALREADY-PERFORMED transaction (state `2` -> `-2`; `design/04` §3.5 reason
    `5` is literally "funds returned"). One caller,
    `payme._cancel_transaction`'s state-`2` branch, already inside its
    transaction — this function neither commits nor checks the state it is
    called for.

    **It RECORDS the reversal; it does not propagate it** (ruling 15, 3.10b).
    Three writes, and deliberately no fourth:

    1. one `correction` allocation per `payment` row this transaction wrote,
       with the sign flipped — the ledger is append-only (ruling 4), so the
       reversal is new rows and the invoice's whole ledger then sums to
       `0.00` rather than claiming money that is gone;
    2. one `reconciliations` row, `result='discrepancy'`, `status='open'` —
       the register a human reads every morning, and the operator's handle on
       a case only a human can finish;
    3. an audit row carrying **RI-01** (`tz/10`: «PAID без подтверждения
       провайдера/банка» — the provider has withdrawn its confirmation and
       the invoice is still `paid`), plus a second one carrying **RI-10**
       («Разрешение активировано без оплаты», critical) when a permit already
       exists for this application.

    **What it does NOT do, and why.** It does not move the invoice off `paid`
    and it does not move the application off `PAID`.
    `applications.service.APPLICATION_TRANSITIONS["PAID"]` is
    `frozenset({"PERMIT_ISSUED"})` and `tz/05` gives PAID no other exit;
    inventing one is a change to the application state machine, owned by
    stage 3.9, which 3.11's issuance gate and 3.11b's revoke both read.
    `design/02` says the same of the invoice — its status is not rewritten,
    the history stays intact. See the public-surface banner at the top of
    this file for the residual window that leaves.

    `reason` is Payme's own cancel reason, carried through into the ledger
    note and the audit row: outside `provider_transactions` this is the only
    place it survives, and it is what tells an accountant "funds returned"
    (`5`) from any other ground.

    Idempotent by inspection, not by a constraint: a `correction` row already
    written against this transaction means this ran before, and a second run
    would negate the ledger twice. `payme`'s own state machine reaches the
    state-`2` branch once per transaction, so this guard is a backstop for a
    manual re-run, never the primary mechanism.
    """
    entries = await repo.list_allocations_by_invoice(db, invoice.id)
    mine = [row for row in entries if row.transaction_id == transaction.id]
    if any(row.entry_type == "correction" for row in mine):
        logger.warning(
            "payments.reversal_already_recorded",
            invoice_id=str(invoice.id),
            transaction_id=str(transaction.id),
        )
        return

    # The rows THIS transaction wrote, not every `payment` row on the invoice:
    # only this transaction's money came back, and negating another
    # transaction's entries would put the ledger further from the truth, not
    # closer. On today's paths the two sets are the same — an invoice is
    # `pending` for exactly one performing transaction — so the invoice's whole
    # ledger does sum back to `0.00`, which is what the test asserts.
    paid_rows = [row for row in mine if row.entry_type == "payment"]
    note = f"reversal of {transaction.provider} transaction {transaction.external_id}"
    if reason is not None:
        note = f"{note} (cancel reason {reason})"
    await repo.add_allocations(
        db,
        [
            Allocation(
                invoice_id=invoice.id,
                transaction_id=transaction.id,
                entry_type="correction",
                target=row.target,
                account=row.account,
                amount=-row.amount,
                note=note,
            )
            for row in paid_rows
        ],
    )

    reversed_amount = sum((row.amount for row in paid_rows), Decimal("0.00"))
    # `difference` here is the money that went back, positive — NOT
    # `matcher.py`'s paid-minus-invoiced convention, which describes a bank
    # line against an invoice and has no bank line to describe on this path.
    # The comment says which of the two a reader is looking at.
    reconciliation = Reconciliation(
        transaction_id=transaction.id,
        invoice_id=invoice.id,
        result=RECONCILIATION_RESULT_DISCREPANCY,
        difference=reversed_amount,
        status=RECONCILIATION_STATUS_OPEN,
        comment=(
            f"{transaction.provider} reversed a confirmed payment of {reversed_amount} "
            f"on invoice {invoice.number} (cancel reason {reason}); the invoice stays "
            f"'{invoice.status}' and the application stays PAID — see payments/service.py"
        ),
    )
    await repo.add_reconciliations(db, [reconciliation])

    # `user_id=None`: there is no HTTP actor on the Payme webhook, the same
    # idiom `payme._change_password` and the scheduled jobs use. `result`
    # stays "success" — the recording succeeded and is legal; the indicator
    # only says a human must look (the shape 3.10b's manual PAID uses, NOT
    # 3.11a's RI-10-on-denial, which is a refusal and commits before raising).
    await audit.log(
        db,
        action=REVERSAL_RECORD,
        user_id=None,
        object_type="invoice",
        object_id=invoice.id,
        old_value={"status": invoice.status},
        new_value={
            "status": invoice.status,
            "transaction_id": str(transaction.id),
            "external_id": transaction.external_id,
            "reason": reason,
            "reversed_amount": str(reversed_amount),
            "reconciliation_id": str(reconciliation.id),
        },
        basis=f"{transaction.provider} cancelled a performed transaction",
        extra={"risk_indicator": RISK_INDICATOR_UNCONFIRMED_PAID},
    )

    # A permit for this application means money has gone back from something
    # already issued — `tz/10`'s RI-10 verbatim. A read-only COUNT, never a
    # call into `permits` (both modules are level 4): see
    # `repo.count_permits_for_application`'s own comment for the boundary
    # argument and for what drops if it is ever re-decided.
    if await repo.count_permits_for_application(db, invoice.application_id) > 0:
        await audit.log(
            db,
            action=REVERSAL_RECORD,
            user_id=None,
            object_type="application",
            object_id=invoice.application_id,
            new_value={
                "invoice_id": str(invoice.id),
                "transaction_id": str(transaction.id),
                "reversed_amount": str(reversed_amount),
                "reconciliation_id": str(reconciliation.id),
            },
            basis="a permit exists for an application whose payment was reversed",
            extra={"risk_indicator": RISK_INDICATOR_PERMIT_WITHOUT_PAYMENT},
        )
