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
- `confirm_payment(db, *, invoice, transaction) -> None` (task 4, ruling J;
  stage 7.9 task 5) — the whole business action behind a confirmed payment:
  invoice -> paid, one ledger row PER RECEIVER written (the frozen split
  re-applied against what actually arrived), application -> PAID, the
  applicant notified, `payment_confirmed` published on the bus. TWO
  callers — `payme._perform_transaction`, already past every
  Payme-protocol check (idempotency, amount, payability), and
  `backoffice_service._confirm_and_pay`, the manual maker-checker door —
  and this function performs exactly ONE check of its own: it refuses
  (`ERR-VAL-001`, before any write) when the frozen split does not fit
  `transaction.amount`, reachable only through the manual door. See its
  own docstring for both callers and the refusal in full.
- `record_reversal(db, *, invoice, transaction, reason) -> None` (3.10b task
  8, ruling 15; ruling #112 added the notify) — the mirror of `confirm_payment`
  for money that came BACK: Payme cancelled an already-performed transaction.
  It RECORDS the reversal (negating `correction` entries, an open
  `reconciliations` row, RI-01 and, when a permit exists, RI-10 plus a
  notification to that permit's `executor_head`) and deliberately moves
  neither the invoice nor the application, and deliberately does not suspend
  the permit either — that stays a person's call. Called from `payme.py`'s
  state-`2` branch ONLY. See the KNOWN GAP paragraph in the public-surface
  comment below for the whole shape of what shipped and what stays open.

`get_invoice_for_actor`/`list_invoices_for_actor`/`create_pay_intent` are
this module's OWN router-facing functions (they take an HTTP `actor: User`,
unlike the functions above) — not part of the cross-module public surface.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import numbers
from app.core.abac import Zone, zone_of
from app.core.errors import err
from app.core.events import Event, publish
from app.core.time import TASHKENT, business_today
from app.modules.admin import repo as admin_repo
from app.modules.admin.models import Organization as OrganizationRow
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.gis import service as gis_service
from app.modules.integrations.adapters import payme as payme_adapter
from app.modules.norms.models import Calculation
from app.modules.notifications import service as notifications_service
from app.modules.payments import events, ledger, repo
from app.modules.payments.models import (
    ALLOCATION_ENTRY_TYPES,
    RECONCILIATION_RESULTS,
    RECONCILIATION_STATUSES,
    SNAPSHOT_KINDS,
    Allocation,
    Invoice,
    InvoiceRecipient,
    PaymentIntent,
    PaymentRecipient,
    ProviderTransaction,
    Reconciliation,
)
from app.modules.payments.permissions import PAYMENTS_CONFIRM, PAYMENTS_VIEW

logger = structlog.get_logger(__name__)

# CLAUDE.md's audit invariant: action codes are "<object>.<verb>" in English,
# and the constant lives with the acting module (mirrors
# applications.service.APPLICATION_STATUS_CHANGE's own idiom).
INVOICE_ISSUE = "invoice.issue"
INVOICE_CANCEL = "invoice.cancel"
INVOICE_PAY = "invoice.pay"
# Ruling #185's own literal, spelled the same way in decisions.md and in the
# plan — NOT this file's usual present-tense idiom (`invoice.issue`,
# `invoice.pay`): the ruling names the audit row itself, so it is copied
# verbatim rather than reshaped to match the sibling constants above.
INVOICE_SETTLE_BY_BENEFIT = "invoice.settled_by_benefit"
# Ruling #202's sibling: the OTHER lawful zero — a statutory exemption
# (`science`, priced `no_tariff_by_law` under the versioned
# `tariff_exempt:<activity>` parameter) — settles the same way, under its
# own audit action so a report can tell a benefit granted from a fee the law
# never set. Same past-participle shape as #185's, for the same reason.
INVOICE_SETTLE_BY_LAW = "invoice.settled_by_law"
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

# Ruling #112: who at the leshoz `record_reversal` notifies when RI-10 fires —
# the person who could actually act on it, `permits.manage`'s own holder
# (`permits/permissions.py`: suspend/resume/revoke, granted to `executor_head`
# alone). A LITERAL, not an import of `app.modules.permits.permissions`: that
# module is level 4, the same level as this one (`design/01` rule 3), and the
# comment on `repo.permit_organization_for_application` already keeps this
# module's one read of `permits` to raw SQL for exactly that reason — a
# permission CODE is no different a cross-level dependency than a model class.
_PERMIT_DECISION_PERMISSION = "permits.manage"

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
ALLOCATION_ENTRY_PAYMENT = ALLOCATION_ENTRY_TYPES[0]
ALLOCATION_ENTRY_CORRECTION = ALLOCATION_ENTRY_TYPES[2]
SNAPSHOT_KIND_REMAINDER = SNAPSHOT_KINDS[2]

# Task 4 (decision #158): `invoice_recipients.name` is NOT NULL even for the
# leshoz's own `kind='remainder'` row — unlike a configured receiver, it has
# no `payment_recipients` row of its own to copy a name from. The leshoz's
# row freezes its ORGANIZATION's own name instead (`_leshoz_snapshot_
# fields`, below `_organization_for`), exactly as a configured receiver's
# row freezes ITS name from `payment_recipients` — this constant is only
# the FALLBACK label for when no organization resolves at all (no contour,
# no assigned organization, no such organization: `_organization_for`'s own
# `None`), which must stay non-fatal — a leshoz without one must never
# block an invoice from being issued (review finding "Important 3").
LESHOZ_SNAPSHOT_NAME: dict[str, Any] = {"uz_latn": "Leshoz"}


# --- Task 7: the public surface for 3.11 permits ---------------------------
#
# Four entry points (stage 7.9 task 4 added the last, `invoice_recipients`),
# and nothing else a level-4+ caller may use to learn about an invoice or its
# ledger. The contract below is FROZEN once this task lands — 3.11 builds
# against it starting now, in a parallel worktree.
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
#   arithmetically true (one `correction` entry per `payment` row that
#   transaction wrote, so its entries cancel out instead of claiming money
#   that is gone — and with it the invoice's whole ledger, for as long as only
#   one transaction can ever perform against an invoice, which is all today's
#   paths allow); the discrepancy register carries an OPEN row whose
#   `difference` is the reversed amount, stored POSITIVE and not in
#   `matcher.py`'s paid-minus-invoiced convention (`record_reversal` explains
#   why the column carries both); RI-01 is raised («PAID без подтверждения
#   провайдера/банка» — the provider withdrew a confirmation the invoice still
#   bears); and RI-10 too («Разрешение активировано без оплаты») when a permit
#   in a LIVE status — `pending_signatures`, `active` or `suspended`, never a
#   `revoked` or `expired` one — already exists for that application. Ruling
#   #112 (7.4d) added the last piece the RI-10 branch was missing: a direct
#   `notify()` to that permit's own `executor_head`, so the decision RI-10
#   flags actually reaches a person instead of waiting for the prosecutor's
#   next sweep or an operator who happens to open the register.
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
# - `invoice_recipients(db, invoice_id) -> list[InvoiceRecipient]` (stage 7.9
#   task 4, decision #158) — the split FROZEN onto this invoice at issuance,
#   `position` order, the leshoz's `kind='remainder'` row always last. The
#   ONE way Tasks 5, 6 and 8 of that plan learn how an invoice divides;
#   reading `payment_recipients` (the LIVE directory) for that question
#   would defeat the freeze the whole `invoice_recipients` table exists for
#   — an admin editing the directory after issuance must not change what an
#   already-issued invoice divides into.
#
# No permission or zone rule on any of the four — the caller is another
# SERVICE inside this process, not an HTTP actor, mirroring
# `applications.service.get`/`norms.service.effective_norm`.
#
# A level-4+ caller must NEVER:
#   - read `invoices`, `allocations` or `invoice_recipients` as tables of
#     its own — no `payments.repo` import, no `select(Invoice)`/
#     `select(Allocation)`/`select(InvoiceRecipient)` against this module's
#     tables from outside it. Every fact reachable that way is already one
#     of the four functions above (module boundary, CLAUDE.md — the same
#     reasoning `norms.service`'s own public-surface comment states for
#     `tariffs`/`rule_parameters`).
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


async def invoice_recipients(db: AsyncSession, invoice_id: uuid.UUID) -> list[InvoiceRecipient]:
    """The split FROZEN onto `invoice_id` at issuance (`issue_invoice`
    below, decision #158), `position` order, the leshoz's own
    `kind='remainder'` row always last. See the banner above — no permission
    or zone rule, the caller is another service — and never read
    `payment_recipients` (the LIVE directory) in this reader's place: that
    would answer "what applies today", not "what this invoice divides
    into", and defeat the whole reason this table exists."""
    return list(await repo.list_invoice_recipients(db, invoice_id))


def missing_payme_receivers(snapshot: Sequence[InvoiceRecipient]) -> list[InvoiceRecipient]:
    """Rows of a frozen `invoice_recipients` snapshot (`invoice_recipients`
    above) that carry no `payme_account_id` AND would actually receive money
    (`row.amount > 0`) — the shared "this split cannot be routed at Payme"
    precondition, decision #160. Scoped to `amount > 0` on purpose:
    `ledger.split_payment` can floor a percent rule, or the leshoz's own
    remainder, to exactly `0.00`, and a receiver Payme would never be asked
    to route anything to can never be the reason the WHOLE payment is
    refused — the same reading Task 6's own `receivers` builder gives a
    `0.00` row (omitted, never routed, but never blocking either).

    TWO callers, the two refusal points Override 1 of the task-6 brief
    names: `create_pay_intent` below (`ERR-PAY-007`, the citizen's own
    button) and `payme.py`'s `_receivers_for` (`-31008`, BOTH
    `CheckPerformTransaction` and `CreateTransaction`) — pure and
    synchronous so neither has to re-derive "missing" its own way and
    silently drift from the other (the same reasoning `resolve_recipient_
    account`'s own docstring gives for staying a single, frozen, two-caller
    helper). An EMPTY snapshot (an invoice issued before stage 7.9, Override
    3) has no rows to iterate and returns `[]` — never a reason to refuse a
    payment nothing here can even describe."""
    return [row for row in snapshot if row.amount > 0 and not row.payme_account_id]


def _free_settlement_benefit_code(calculation: Calculation) -> str | None:
    """Ruling #185's own condition, checked ONCE, here: `calculation.amount
    == 0` AND its `breakdown` (`norms.calculator._apply_benefit`) carries at
    least one `{"kind": "benefit", ...}` line whose `modifier` is exactly
    zero. Returns that line's own `code` — the benefit actually granted —
    or `None`.

    **Compared as `Decimal`, never as the STRING `_apply_benefit` itself
    writes** (`str(modifiers[benefit_code])`, which every #181 row seeds as
    `"0"` but could as well be written `"0.000000"` or `"0E-6"` by a future
    admin edit) — `"0" == "0.0"` is `False` as strings and `True` as
    `Decimal`. A line whose `modifier` fails to parse is skipped, not
    fatal: `breakdown` is JSONB on an APPEND-ONLY table, and a row from a
    calculator shape older than this ruling must still issue an invoice,
    just never for free.

    **`None` for every OTHER zero — the fail-closed half of the ruling.**
    A tariff published at a genuine zero coefficient, or a unit nobody has
    priced yet, carries no `benefit` line at all, and this returns `None`
    so `issue_invoice` keeps issuing the plain `pending` invoice it always
    has — loud in the accountant's list, never silently free."""
    if calculation.amount != 0:
        return None
    for line in calculation.breakdown or []:
        if not isinstance(line, dict) or line.get("kind") != "benefit":
            continue
        modifier = line.get("modifier")
        if modifier is None:
            continue
        try:
            is_zero = Decimal(str(modifier)) == 0
        except ArithmeticError:  # InvalidOperation is one of these
            continue
        if is_zero:
            code = line.get("code")
            if isinstance(code, str):
                return code
    return None


async def _verified_claim_of(
    db: AsyncSession, application: Any, benefit_code: str
) -> BenefitClaim | None:
    """Ruling #185's missing half (stage 10 review, finding 1): the benefit
    the CALCULATION priced must be the benefit the APPLICATION claimed and
    somebody verified — the leshoz inside the review, or the Union register
    at filing (#182). Returns the claim (code and the category's own name,
    for the applicant's notice) or `None` when the application's claim is
    absent, unverified, rejected, or a different category than the line."""
    if application.benefit_verification_status != "verified":
        return None
    item_id = application.benefit_category_item_id
    if item_id is None:
        return None
    item = await admin_repo.get_classifier_item(db, item_id)
    if item is None or item.code != benefit_code:
        return None
    return BenefitClaim(code=item.code, name=str((item.name or {}).get("uz_latn") or item.code))


@dataclass(frozen=True)
class BenefitClaim:
    code: str
    name: str


def _exempt_activity_code(calculation: Calculation) -> str | None:
    """Ruling #202's own condition, the twin of `_free_settlement_benefit_
    code` above: `calculation.amount == 0` AND its `breakdown` carries a
    `{"kind": "tariff", "reason": "no_tariff_by_law", ...}` line — the
    statement `norms.calculator.calculate` writes ONLY when the versioned
    `tariff_exempt:<activity>` parameter is published as `"true"` (a merely
    missing tariff row raises `ERR-NORM-004` instead), so the line is a
    fact of law, never an accident of an empty table. Returns that line's
    own `activity_code`, or `None` for every other zero — the same
    fail-closed half #185 keeps for itself."""
    if calculation.amount != 0:
        return None
    for line in calculation.breakdown or []:
        if not isinstance(line, dict) or line.get("kind") != "tariff":
            continue
        if line.get("reason") != "no_tariff_by_law":
            continue
        code = line.get("activity_code")
        if isinstance(code, str):
            return code
    return None


@dataclass(frozen=True)
class StatutoryExemption:
    activity_code: str
    activity_name: str


async def _exemption_of(
    db: AsyncSession, application: Any, activity_code: str
) -> StatutoryExemption | None:
    """Ruling #202's pairing, mirroring `_verified_claim_of`: the activity
    the CALCULATION was priced exempt for must be the APPLICATION's own
    `activity_type_id`. `norms.service.save_calculation` binds a
    calculation to the application's contour only (ruling 20), so a head
    could bind one the calculator priced as `science` to a grazing
    application, and a `no_tariff_by_law` line alone proves nothing about
    THIS application. Returns the exemption (code and the activity's own
    name, for the applicant's notice) or `None` — an application with no
    activity, or a different one, keeps its plain `pending` invoice."""
    activity_type_id = application.activity_type_id
    if activity_type_id is None:
        return None
    activity = await admin_repo.get_activity_type(db, activity_type_id)
    if activity is None or activity.code != activity_code:
        return None
    return StatutoryExemption(
        activity_code=activity.code,
        activity_name=str((activity.name or {}).get("uz_latn") or activity.code),
    )


async def _settle_free(
    db: AsyncSession, *, invoice: Invoice, reason: BenefitClaim | StatutoryExemption
) -> None:
    """Rulings #185 and #202: a zero-sum invoice that is lawfully zero
    settles ITSELF, in the SAME transaction `issue_invoice` created it in —
    called only from there, after the invoice row (and its
    `invoice_recipients` snapshot) already exist, so reports keep counting
    what was granted free and why even though nothing was ever billed.
    `reason` is the verified benefit claim (#185) or the statutory
    exemption (#202); the two differ ONLY in the audit action and in the
    notice the applicant reads — everything that moves state is shared.

    Writes exactly what a confirmed payment writes, MINUS the two things
    that require money to have actually moved: `invoice.status = 'paid'`,
    `invoice.paid_at = now()` (ruling #185's own words — a fresh clock read,
    not a reuse of `issued_at`: the two are the same TRANSACTION but not
    the same instant, and `confirm_payment`'s own `paid_at` is likewise the
    moment of confirmation, never the moment of issuance), the application
    `INVOICED -> PAID` through the SAME `applications.service.set_status`
    `confirm_payment` calls (never a second way to reach PAID), and
    `payment_confirmed` published with the IDENTICAL payload shape
    `confirm_payment` publishes — two identifiers, nothing else — so
    `permits.subscribers.on_payment_confirmed` cannot tell the two apart.
    Deliberately does NOT write a `provider_transactions` row and does NOT
    call `ledger.split_payment`/`repo.add_allocations`: nothing arrived, so
    there is nothing to divide — a ledger entry here would claim money that
    was never seen, and `service.is_settled_without_payment` below reads the
    resulting ABSENCE of a `provider_transactions` row as this function's
    own signature."""
    invoice.status = "paid"
    invoice.paid_at = datetime.now(UTC)

    if isinstance(reason, BenefitClaim):
        audit_action = INVOICE_SETTLE_BY_BENEFIT
        audit_value: dict[str, str] = {"benefit_code": reason.code}
        notice_code = events.INVOICE_SETTLED_BY_BENEFIT
        # The category's own name, never the code: «To'lov talab
        # qilinmaydi: war_veterans» is what the review found in the SMS.
        notice_params: dict[str, Any] = {"benefit": reason.name}
    else:
        audit_action = INVOICE_SETTLE_BY_LAW
        audit_value = {"activity_code": reason.activity_code}
        notice_code = events.INVOICE_SETTLED_BY_LAW
        notice_params = {"activity": reason.activity_name}

    await audit.log(
        db,
        action=audit_action,
        object_type="invoice",
        object_id=invoice.id,
        new_value=audit_value,
    )

    application = await applications_service.set_status(
        db, invoice.application_id, to_status="PAID"
    )

    await notifications_service.notify(
        db,
        event_code=notice_code,
        recipient_user_id=application.submitted_by_user_id,
        params={"invoice_number": invoice.number, **notice_params},
        object_type="invoice",
        object_id=invoice.id,
    )

    # Same bus name, same two-key payload `confirm_payment` publishes below
    # (`events.PAYMENT_CONFIRMED`, "no second source of truth for money" —
    # ruling 15) — `permits.subscribers.on_payment_confirmed` reads
    # `application_id` off this event and must not be able to tell a free
    # settlement from a real one.
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


async def is_settled_without_payment(db: AsyncSession, invoice: Invoice) -> bool:
    """Rulings #185 and #202: whether `invoice` was settled through
    `_settle_free` above — by a verified benefit OR a statutory exemption,
    which this reader deliberately does not tell apart (the audit row does)
    — so the cabinet and the register can say "nothing to pay" without
    parsing audit rows. Renamed from `is_settled_by_benefit` when #202 made
    that name a lie for a `science` invoice. Named WITHOUT a leading
    underscore since it is read from `router.py` across files, the same
    convention `holds_payments_view`'s own docstring states.

    **The cheapest honest derivation** (plan B4), checked in this order so
    the database is asked only for the rare row that is actually a
    candidate: `invoice.status == "paid"` AND `invoice.amount == 0` — the
    ONLY zeros this module ever settles for free (the fail-closed half of
    both rulings: no other zero ever reaches `status='paid'`, since Payme's
    own `-31001` pins `transaction.amount == invoice.amount` and
    `ManualConfirmationIn.amount` is bounded `gt=0`) — AND no `provider_
    transactions` row exists for it. A real payment, Payme's or the manual
    maker-checker door's synthetic `provider='manual'` row alike, ALWAYS
    writes one (`confirm_payment`'s own docstring); `_settle_free` never
    does. Every other invoice answers `False` from its own already-loaded
    columns, with no query at all."""
    if invoice.status != "paid" or invoice.amount != 0:
        return False
    return not await repo.has_provider_transaction(db, invoice.id)


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

    Stage 7.9 task 4 (decision #158): also freezes the split onto the new
    invoice — `payment_recipients`' ACTIVE rows, applied through
    `ledger.split_payment` and written as `invoice_recipients`, so an
    administrator editing the directory afterwards cannot change what THIS
    invoice divides into (`invoice_recipients` mirrors `calculation_id`
    above for the same reason). This write sits on the branch that CREATES a
    new invoice — after the early idempotency return above, never on it — so
    a retry inside the same approval transaction (the same retry the
    docstring above already accounts for) can never write the snapshot
    twice and trip `uq_invoice_recipients_position`. A configuration whose
    fixed amounts alone exceed the invoice refuses the WHOLE issuance
    (`ledger.SplitDoesNotFit` -> `ERR-VAL-001`) rather than issuing an
    invoice nobody could divide.

    Ruling #185, checked last, on the branch that CREATES a new invoice:
    when `calculation.amount == 0` AND its own `breakdown` carries a
    `benefit` line whose `modifier` is zero (`_free_settlement_benefit_
    code`), the invoice settles ITSELF in this same transaction
    (`_settle_free`) instead of sitting `pending` for a payment nobody can
    ever make — every #181 benefit prices at exactly zero, and `Manual
    ConfirmationIn.amount` is bounded `gt=0`. Any OTHER zero (a genuine
    zero tariff, an unpriced unit) carries no such line and issues the
    plain `pending` invoice unchanged — the fail-closed half of the ruling.
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

    # The split, frozen onto THIS invoice (decision #158). `repo.
    # list_active_recipients` — the ACTIVE directory rows, `(sort_order,
    # id)` order — is read DIRECTLY and exactly ONCE here, never through
    # `recipients_service` (whose own readers return either
    # `PaymentRecipientOut` schemas or, formerly, `RecipientRule` tuples
    # with no name to copy — neither can supply what this snapshot needs):
    # `rules` (the engine's input) and the name/`payme_account_id` each
    # receiver row copies (below, `_snapshot_rows`) both come out of this
    # one read, never two separate queries a concurrent directory edit
    # could answer differently (lesson: "a precondition shared by several
    # steps belongs in ONE function every step calls").
    recipients = await repo.list_active_recipients(db)
    rules = [
        ledger.RecipientRule(row.id, row.kind, row.percent, row.fixed_amount) for row in recipients
    ]
    try:
        shares = ledger.split_payment(invoice.amount, rules)
    except ledger.SplitDoesNotFit as exc:
        # `ERR-VAL-001` (422), the same code and the same reasoning as the
        # `calculation is None` branch above: this function has no HTTP
        # route of its own, it runs as a bus subscriber INSIDE the
        # publisher's transaction, so the approval rolls back WITH it
        # either way (synchronous, in-transaction bus, by design) — an
        # invoice nobody can divide must never exist, and the reviewer
        # needs to be told WHY here rather than discovering it as a 500
        # out of `confirm_payment` days later, when the money has already
        # arrived and there is nowhere left for the excess to come from.
        raise err(
            "ERR-VAL-001",
            details={"reason": "split_does_not_fit", "detail": str(exc)},
        ) from exc

    leshoz = await _leshoz_snapshot_fields(
        db, contour_id=application.contour_id, assigned_org_id=application.assigned_org_id
    )
    await repo.add_invoice_recipients(db, _snapshot_rows(invoice, recipients, shares, leshoz))

    # Ruling #185, checked LAST: the invoice above is issued in FULL either
    # way — the recipients snapshot just frozen included, so reports keep
    # counting what was granted free and under which category — and only
    # what happens NEXT forks. `_free_settlement_benefit_code` re-reads
    # `calculation.amount`/`.breakdown` rather than trusting a flag, so a
    # calculation whose OWN numbers do not carry a zero-modifier benefit
    # line NEVER takes this branch, whatever its `amount` happens to be —
    # the fail-closed half of the ruling.
    benefit_code = _free_settlement_benefit_code(calculation)
    claimed = await _verified_claim_of(db, application, benefit_code) if benefit_code else None
    # Ruling #202, the other lawful zero, paired with the application's own
    # activity the same way the benefit is paired with its claim.
    exempt_code = _exempt_activity_code(calculation)
    exemption = await _exemption_of(db, application, exempt_code) if exempt_code else None
    if benefit_code is not None and claimed is None:
        # Stage 10 review, finding 1 — the hiding shape again. A head can
        # POST a calculation with ANY `benefit_code` (`norms` binds it to
        # the contour only, ruling 20), so a zero-sum calculation is not
        # proof the APPLICATION earned it: this one claimed nothing, or its
        # claim is not `verified`, or it claimed a different category. The
        # invoice stays `pending` at 0 — exactly as loud as before #185 —
        # and the log says why; nothing is granted free on a line somebody
        # typed.
        logger.warning(
            "payments.free_settlement_refused",
            invoice_id=str(invoice.id),
            application_id=str(application.id),
            benefit_code=benefit_code,
            claim_status=application.benefit_verification_status,
        )
    if exempt_code is not None and exemption is None:
        logger.warning(
            "payments.free_settlement_refused",
            invoice_id=str(invoice.id),
            application_id=str(application.id),
            exempt_activity_code=exempt_code,
            application_activity_type_id=str(application.activity_type_id),
        )
    if claimed is not None:
        await _settle_free(db, invoice=invoice, reason=claimed)
    elif exemption is not None:
        await _settle_free(db, invoice=invoice, reason=exemption)
    else:
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


class _LeshozSnapshotFields(NamedTuple):
    """What `issue_invoice` freezes onto the leshoz's own `kind='remainder'`
    row: its `name` (a `LocalizedName`-shaped dict, `app/modules/admin/
    models.py::Organization.name`) and its Payme account id — both resolved
    by `_leshoz_snapshot_fields`, below `_organization_for`."""

    name: dict[str, Any]
    payme_account_id: str | None


def _snapshot_rows(
    invoice: Invoice,
    recipients: Sequence[PaymentRecipient],
    shares: Sequence[ledger.Share],
    leshoz: _LeshozSnapshotFields,
) -> list[InvoiceRecipient]:
    """`issue_invoice`'s own pure, in-memory builder — no I/O, no session,
    nothing here can fail once its inputs are already consistent.

    `recipients` is exactly the ACTIVE directory `rules` (the engine's
    input `issue_invoice` built `shares` from) was itself built from, in the
    SAME `(sort_order, id)` order — so `recipients[i]` and `shares[i]` name
    the SAME recipient in the SAME position, which `zip(..., strict=True)`
    below asserts rather than trusts by convention. `shares` carries exactly
    one MORE entry than `recipients`: `ledger.split_payment` always appends
    the leshoz's own `Share` last, `recipient_id=None` (Override 2 of this
    task's brief) — the loop below stops one short of `shares` and the
    leshoz's own row is appended separately, `kind='remainder'`.

    Each receiver row copies its OWN `name`/`payme_account_id` from its
    `payment_recipients` row — never re-derives them — because that row may
    already have moved on by the time anyone reads this snapshot back
    (decision #158, the whole point of freezing it). The leshoz's row has no
    `payment_recipients` row of its own to copy from: its `name` and
    `payme_account_id` come from `leshoz` (`_leshoz_snapshot_fields`,
    resolved by the caller and handed in already-resolved, since that
    resolution needs a session and this function may not take one) — its
    OWN organization's name when one resolves, `LESHOZ_SNAPSHOT_NAME`
    otherwise (review finding "Important 3")."""
    leshoz_share = shares[-1]
    rows: list[InvoiceRecipient] = []
    for position, (recipient, share) in enumerate(zip(recipients, shares[:-1], strict=True)):
        assert recipient.id == share.recipient_id, (
            f"recipients[{position}]={recipient.id} does not match "
            f"shares[{position}].recipient_id={share.recipient_id} — issue_invoice "
            "must build `rules` from `recipients` in the same order"
        )
        rows.append(
            InvoiceRecipient(
                invoice_id=invoice.id,
                recipient_id=recipient.id,
                name=recipient.name,
                payme_account_id=recipient.payme_account_id,
                kind=recipient.kind,
                percent=recipient.percent,
                fixed_amount=recipient.fixed_amount,
                amount=share.amount,
                position=position,
            )
        )
    rows.append(
        InvoiceRecipient(
            invoice_id=invoice.id,
            recipient_id=None,
            name=leshoz.name,
            payme_account_id=leshoz.payme_account_id,
            kind=SNAPSHOT_KIND_REMAINDER,
            percent=None,
            fixed_amount=None,
            amount=leshoz_share.amount,
            position=len(rows),
        )
    )
    return rows


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


async def holds_payments_read(db: AsyncSession, actor: User) -> bool:
    """Holds a permission that entitles its holder to READ invoices —
    `payments.view`, or `payments.confirm`, or the superuser gate.

    `payments.confirm` is here because of the stage 7.3 walkthrough (finding
    F13): the checker of a manual `PAID` could list the confirmations awaiting
    them and could open neither the invoice being confirmed nor the bank
    document behind it, so the second pair of eyes in a four-eyes control was
    asked to approve blind. A permission answers "whether", the zone still
    answers "whose" — `_may_act_on_invoices_of` applies
    `_zone_covers_application` to both codes alike, so a head reads their own
    leshoz's invoices and no one else's.

    **Reads only.** `create_pay_intent` shares `_may_act_on_invoices_of` and
    deliberately does NOT accept this wider set: raising a payment link is
    `payments.view`'s, and the caller marks which question it is asking with
    `read_only`.

    Named WITHOUT a leading underscore since the whole-branch review's
    Important 2: `router.py::_invoice_out` gained a SECOND caller across
    files, to decide whether `GET /invoices/{id}` attaches `recipients` at
    all — it must be the SAME predicate `_may_act_on_invoices_of`'s staff
    branch already uses to decide who may act on the invoice in the first
    place (`staff = holds_payments_read if read_only else
    holds_payments_view`, right below), or a `payments.confirm`-only holder
    (`executor_head`, the checker half of the maker-checker PAID, and the
    head of the leshoz that receives the invoice's own remainder) reaches
    the route and gets a body with `recipients` silently absent — not a
    403, not a null: gone. One access rule, one source; `holds_payments_view`
    alone was a SECOND, narrower copy of it living in the wrong file."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    codes = await auth_repo.permission_codes(db, actor)
    return PAYMENTS_VIEW in codes or PAYMENTS_CONFIRM in codes


async def holds_payments_view(db: AsyncSession, actor: User) -> bool:
    """Holds `payments.view`, or is the superuser that passes every permission
    gate (decision #41 ruling 2) — the same two-branch shape
    `norms.service._holds_tariffs_publish`/`gis.service._may_manage_layers`
    use for a rule INSIDE a handler, as opposed to a `require_permission`
    dependency on the route itself (needed here because even a caller
    holding NEITHER `payments.view` NOR any grant at all must still reach
    these routes, to read their OWN invoice).

    Named WITHOUT a leading underscore since stage 7.9 task 8, when
    `router.py` gained a caller across files — the same "no underscore
    once a caller crosses a file" convention `resolve_recipient_account`'s
    own docstring states. That caller (`_invoice_out`, deciding whether
    `GET /invoices/{id}` attaches `recipients` at all) has since moved to
    `holds_payments_read` instead (whole-branch review Important 2 —
    gating on THIS narrower predicate silently dropped `recipients` for a
    `payments.confirm`-only reader the route otherwise treats as staff);
    the underscore stays off regardless, because `_may_act_on_invoices_of`
    right below still calls this across the SAME two-branch shape for the
    write path."""
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return PAYMENTS_VIEW in await auth_repo.permission_codes(db, actor)


def _organization_in_zone(zone: Zone, org: OrganizationRow) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL for ONE organization row — a
    LOCAL copy of the identical helper in `gis.service` and `norms.service`,
    for the same reason they keep their own: it is not part of either module's
    declared public surface, and a level-4 module may not import it."""
    if zone.region_id is not None and zone.region_id != org.region_id:
        return False
    if zone.district_id is not None and zone.district_id != org.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != org.id:
        return False
    return True


# `list_invoices_for_actor`'s browse-all path (backend-gaps finding 3) used
# to scan only the `_INVOICES_ZONE_SCAN_CAP` newest rows per request when the
# actor was zone-scoped, mirroring `backoffice_service.
# _MANUAL_CONFIRMATIONS_ZONE_SCAN_CAP`. That sibling's own justification —
# "a maker files these one at a time, a real worklist is small" — does not
# transfer here: invoices are the system's core document, issued once per
# approved application NATIONWIDE, so a fixed window was not a safety bound,
# it was a silent, permanent blind spot. Once national volume for a status
# passed the cap, a leshoz whose own invoices were not among the nationally
# newest `_INVOICES_ZONE_SCAN_CAP` saw none of them, on no page, ever — and
# `total` undercounted to match (backend-gaps review, 2026-09-06). Fixed by
# scanning the full matching-status set instead of a fixed window — see
# `_scan_invoices_in_zone`.


async def _zone_covers_application(
    db: AsyncSession, application_id: uuid.UUID, *, actor: User
) -> bool:
    """Whether this actor's territory contains the invoice's application.

    `tz/12` #35, answered by Oybek on 2026-09-05: an accountant belongs to a
    leshoz. Until then `payments.view` alone opened any invoice in the country
    and the same predicate guarded the pay-intent route, so an accountant of
    one leshoz could open AND PAY another's — the one place in the system where
    territorial scoping did not hold.

    An empty zone still means the whole republic, which is what `sys_admin` has
    and what a CENTRAL accountant is given deliberately: the answer makes the
    republic-wide case an explicit empty zone rather than the only behaviour
    available.

    **Fails closed.** An application nothing can place in a zone — no assigned
    organization and no contour — is refused to a zoned actor rather than
    shown. That state exists (a draft names no contour), and "unplaceable"
    must never read as "everyone's".
    """
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return True
    organization_id = await applications_service.effective_organization(db, application_id)
    if organization_id is None:
        return False
    org = await admin_repo.get_organization(db, organization_id)
    return org is not None and _organization_in_zone(zone, org)


async def _may_act_on_invoices_of(
    db: AsyncSession,
    application_id: uuid.UUID,
    applicant_id: uuid.UUID,
    *,
    actor: User,
    read_only: bool = False,
) -> bool:
    """`payments.view` (or sys_admin) sees or pays any invoice; a
    `payments.confirm` holder SEES one (`read_only=True`, finding F13) and
    still may not raise a payment link with it; otherwise the
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
    staff = holds_payments_read if read_only else holds_payments_view
    if await staff(db, actor):
        # A permission says WHETHER, a zone says WHERE — and zone scoping is
        # not a permission check (lesson). Staff pass both or neither.
        return await _zone_covers_application(db, application_id, actor=actor)
    # The citizen's own branch is deliberately untouched by the zone: ownership
    # is not territorial, and a zone rule reaching it would hide a person's own
    # bill from them.
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
    if not await _may_act_on_invoices_of(
        db, application.id, application.applicant_id, actor=actor, read_only=True
    ):
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    return invoice


async def list_invoices_for_actor(
    db: AsyncSession,
    application_id: uuid.UUID | None,
    *,
    actor: User,
    status: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Invoice], int]:
    """`GET /invoices` — with `?application_id=`, the original rule: same
    ownership-or-representation-or-staff-in-zone check as
    `get_invoice_for_actor`, applied to the application rather than one
    invoice, and the same 404-not-403 reasoning (a stranger asking about an
    application that is not theirs cannot tell it apart from one that does
    not exist at all).

    Without it (backend-gaps finding 3), the register itself: a `payments.
    view` holder browses every invoice, not one application's own — the
    citizen's branch above has no republic to browse, so this half is
    staff-only, `ERR-ACL-001` for anyone else, the same shape
    `list_manual_confirmations`/`list_reconciliations` already gate on
    `PAYMENTS_VIEW`/`PAYMENTS_CONFIRM`. Zone-scoped like every other list in
    this system (decision #70, fails closed): an empty zone (a central
    accountant, `sys_admin`) pages straight out of SQL — the common case
    costs no extra query — a leshoz-scoped one goes through
    `_scan_invoices_in_zone`, which walks the WHOLE matching-status set and
    filters it per row through the SAME `_zone_covers_application` the
    single-invoice routes use, because `invoices` carries no
    `organization_id` of its own to filter on in SQL (the same reason
    `list_manual_confirmations` scans instead of filtering). See that
    function's own docstring for why this is a full scan rather than a
    capped one, and what that costs. Always still reachable by id or by
    `?application_id=` regardless."""
    if application_id is not None:
        application = await applications_service.get(db, application_id)
        if application is None:
            raise err("ERR-SYS-003", details={"application": str(application_id)})
        if not await _may_act_on_invoices_of(
            db, application.id, application.applicant_id, actor=actor, read_only=True
        ):
            raise err("ERR-SYS-003", details={"application": str(application_id)})
        return await repo.list_invoices_by_application(
            db, application_id, status=status, limit=limit, offset=offset
        )

    if not await holds_payments_read(db, actor):
        raise err("ERR-ACL-001")
    zone = zone_of(actor)
    if zone == Zone(None, None, None):
        return await repo.list_invoices(db, status=status, limit=limit, offset=offset)
    visible = await _scan_invoices_in_zone(db, status=status, actor=actor)
    return visible[offset : offset + limit], len(visible)


async def _scan_invoices_in_zone(
    db: AsyncSession, *, status: str | None, actor: User
) -> list[Invoice]:
    """The full per-row zone scan `list_invoices_for_actor`'s browse-all path
    needs: every invoice matching `status` (all of them, if `None`), newest
    first, filtered through `_zone_covers_application` — because `invoices`
    carries no `organization_id` of its own to filter on in SQL, and
    `payments` may not join into `applications`' tables to build one (module
    boundary).

    **No fixed window.** An earlier version capped this at the
    `_INVOICES_ZONE_SCAN_CAP` most recent rows, mirroring
    `backoffice_service._MANUAL_CONFIRMATIONS_ZONE_SCAN_CAP` — cheap, but
    wrong: that sibling's cap holds because a maker files those confirmations
    ONE AT A TIME, so a real worklist stays small, and invoices do not share
    that property — they are the system's core document, issued once per
    approved application NATIONWIDE. Once national volume for a status
    passed the cap, a leshoz's own invoice older than the window became
    invisible on this route FOREVER, on every page, with `total` silently
    undercounting to match (backend-gaps review, 2026-09-06) — not a
    performance trade-off, a correctness bug. So this reads the whole
    matching-status set in one query rather than a bounded one: for a table
    with no zone column and no cross-module join available, that is the only
    way to answer BOTH "which of these are mine" and "how many" correctly.

    **The honest cost.** This trades a bounded-but-wrong-forever read for one
    whose cost grows with national invoice volume for the given `status`,
    still one query plus one `_zone_covers_application` await per row (the
    same per-row shape the capped version already had). Reading every row in
    a single query is deliberately cheaper here than chunking it: an
    OFFSET-paginated re-scan would ask Postgres to re-sort the same
    unindexed matching set from scratch on every batch, which costs MORE
    overall than one sort, not less. If national invoice volume per status
    ever makes this scan itself too slow, the durable fix is a denormalized
    zone/organization column on `invoices` (a schema change, its own
    reviewed plan) — not a smaller cap here, which is the exact bug this
    replaces."""
    rows = await repo.list_invoices_matching(db, status=status)
    return [
        row for row in rows if await _zone_covers_application(db, row.application_id, actor=actor)
    ]


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

    Stage 7.9 task 6 (decision #160): checked LAST, after every other
    precondition above, is whether the invoice's own FROZEN split
    (`invoice_recipients`, decision #158) can be routed at Payme at all —
    every row that would actually receive money (`amount > 0`) must carry a
    `payme_account_id`, or this refuses with `ERR-PAY-007` and
    `details.missing` naming the receivers that lack one **by POSITION
    ONLY, never by name** (whole-branch review Important 4, fixed from
    `{"position", "name"}`): this route answers the APPLICANT's own "pay"
    button, and `GET /invoices/{id}` already hides `recipients` from that
    same actor (Override 4) — echoing a receiver's name back into a 409 the
    instant they press pay would hand them exactly what the read route
    refuses to. `logger.error` right below names them IN FULL for staff,
    who are who must actually fix a missing `payme_account_id` — decision
    #160's own goal (a legible refusal rather than a raw provider error)
    is served by `position` alone: it tells the reader WHICH configured
    row is broken without disclosing WHO it is to the one actor who must
    never be told. This is the refusal a human actually SEES (Override 1
    of the task's own brief) — Payme's own `CheckPerformTransaction`/
    `CreateTransaction` (`payme.py`) answer the SAME condition as `-31008`
    instead, for a citizen who reaches the payment page some other way.
    All-or-nothing by construction (`missing_payme_receivers` reads the
    WHOLE snapshot): a partial `receivers` array would route some
    receivers at Payme and leave the rest on the Agency's own cashbox
    awaiting a manual transfer — the worst of both mechanisms and the
    hardest thing in this system to reconcile. An invoice issued BEFORE
    this stage carries an EMPTY snapshot (Override 3) and stays payable
    unchanged — refusing those would strand every invoice already pending
    on the stand.
    """
    invoice = await repo.get_invoice(db, invoice_id)
    if invoice is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if not await _may_act_on_invoices_of(db, application.id, application.applicant_id, actor=actor):
        raise err("ERR-SYS-003", details={"invoice": str(invoice_id)})
    if invoice.status != "pending":
        raise err("ERR-PAY-004", details={"invoice": str(invoice_id), "status": invoice.status})
    if datetime.now(UTC) > invoice.due_at:
        raise err("ERR-PAY-002", details={"invoice": str(invoice_id)})

    snapshot = await invoice_recipients(db, invoice.id)
    missing = missing_payme_receivers(snapshot)
    if missing:
        logger.error(
            "payments.pay_intent_refused_unroutable_split",
            invoice_id=str(invoice_id),
            missing=[{"position": row.position, "name": row.name} for row in missing],
        )
        raise err(
            "ERR-PAY-007",
            details={
                "invoice": str(invoice_id),
                "missing": [{"position": row.position} for row in missing],
            },
        )

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


async def _organization_for(
    db: AsyncSession, *, contour_id: uuid.UUID | None, assigned_org_id: uuid.UUID | None
) -> OrganizationRow | None:
    """The leshoz's own organization row — `contour_id` ->
    `gis.service.contour_organization` -> `admin.repo.get_organization`,
    falling back to `assigned_org_id` when there is no contour. `None`
    whenever any step comes up empty: no contour AND no assigned
    organization, a contour with no known owner, or an owner id that no
    longer resolves.

    Extracted (stage 7.9 task 4) so `resolve_recipient_account` and
    `_leshoz_snapshot_fields` below — both of which walk this SAME
    four-step chain to read the SAME organization's `requisites` and/or
    `name` — cannot drift apart. Two functions independently walking
    the same steps is exactly the shape that produced finding F7 of stage
    7.4: a module's own hard-coded claim about another module rots silently
    once nothing forces the two claims to agree."""
    org_id: uuid.UUID | None
    if contour_id is not None:
        org_id = await gis_service.contour_organization(db, contour_id)
    else:
        org_id = assigned_org_id
    if org_id is None:
        return None
    return await admin_repo.get_organization(db, org_id)


async def resolve_recipient_account(
    db: AsyncSession, *, contour_id: uuid.UUID | None, assigned_org_id: uuid.UUID | None
) -> str | None:
    """Ruling H: the leshoz's own bank account for its own remainder share
    (`target=TARGET_RECIPIENT` — a configured receiver resolves a Payme
    WALLET elsewhere, never through this function) —
    `_organization_for(...)` -> `organization.requisites.get("account")`.
    `None` (never a placeholder string) whenever any step comes up empty: a
    leshoz without bank details, or without even an assigned organization,
    must never block money that has already arrived — `allocations.account`
    is nullable for exactly this (Task 3 ruling).

    Takes the two fields it actually reads, not the whole `Application`
    (review finding I3): `payments` may not import `applications.models` —
    CLAUDE.md's module boundary, and the ONLY import of it anywhere in
    `app/` outside `applications` itself and `models_registry.py` before
    this fix. `confirm_payment` (the caller) already holds the row via
    `applications.service.get`, itself the sanctioned cross-module surface —
    only the TYPE import for this helper's own signature was the violation.

    **Signature and `None`-on-any-missing-step behaviour are FROZEN as of
    stage 7.9 task 4.** TWO existing callers depend on both: `confirm_payment`
    (below, same file) and `backoffice_service.approve_refund`.

    Named WITHOUT a leading underscore since 3.10b task 9: it gained a
    SECOND caller in that stage, `backoffice_service.approve_refund`, which
    resolves the very same account for a refund's recipient-side entry — a
    module-internal helper with two callers across two files of the SAME
    module is exactly what the rest of this file (`invoice_for_application`,
    `allocations_for`, ...) already spells with no underscore; only a
    helper that stays single-file-private keeps one. `holds_payments_view`
    lost its own underscore the same way in stage 7.9 task 8, once
    `router.py` needed it too, to decide whether `GET /invoices/{id}`
    attaches the split at all — `_may_act_on_invoices_of` right below is
    still called from nowhere but this file, so it is the one that keeps
    its underscore today."""
    organization = await _organization_for(
        db, contour_id=contour_id, assigned_org_id=assigned_org_id
    )
    if organization is None:
        return None
    account = organization.requisites.get("account")
    return account if isinstance(account, str) else None


async def _leshoz_snapshot_fields(
    db: AsyncSession, *, contour_id: uuid.UUID | None, assigned_org_id: uuid.UUID | None
) -> _LeshozSnapshotFields:
    """The leshoz's own name and Payme account id, frozen onto an invoice's
    `invoice_recipients` remainder row at issuance (decision #158, review
    finding "Important 3") — both read off the SAME `_organization_for(...)`
    row, in ONE query, the same chain `resolve_recipient_account` reads
    (there `organization.requisites.get("account")`, here `.get(
    "payme_account_id")` and `.name` directly).

    `None`/`LESHOZ_SNAPSHOT_NAME` whenever `_organization_for` comes up
    empty — no contour AND no assigned organization, a contour with no
    known owner, or an owner id that no longer resolves — exactly the same
    posture `resolve_recipient_account` already states for the recipient
    account: a leshoz with no organization, or one with no Payme id
    configured, must never block an invoice from being issued, only leave
    that invoice's leshoz row without one (and, for the name, carrying the
    fixed fallback label instead of the organization's own)."""
    organization = await _organization_for(
        db, contour_id=contour_id, assigned_org_id=assigned_org_id
    )
    if organization is None:
        return _LeshozSnapshotFields(name=LESHOZ_SNAPSHOT_NAME, payme_account_id=None)
    payme_account_id = organization.requisites.get("payme_account_id")
    return _LeshozSnapshotFields(
        name=organization.name,
        payme_account_id=payme_account_id if isinstance(payme_account_id, str) else None,
    )


async def confirm_payment(
    db: AsyncSession, *, invoice: Invoice, transaction: ProviderTransaction
) -> None:
    """The whole business action behind a confirmed payment — invoice ->
    paid, one ledger row PER RECEIVER written, application -> PAID, the
    applicant notified, `payment_confirmed` published — all inside the
    CALLER's transaction: the caller owns the session, and this function
    commits nothing of its own. Every PRECONDITION on the caller's side —
    idempotency, amount, payability, the invoice's own status — is the
    caller's, never repeated here.

    **One check IS this function's own, and it is not a precondition — it
    is the split arithmetic itself.** Task 5 added it (see the `ERR-VAL-001`
    paragraph further down this same docstring): `ledger.split_payment`
    raises `SplitDoesNotFit` when the frozen rules do not fit `transaction.
    amount`, and this function turns that into `ERR-VAL-001` before
    anything is written. That is not a business rule a caller could have
    checked in advance (it depends on the split, which only this function
    reads) — it is this function refusing to allocate money it cannot
    honestly divide, the same posture `issue_invoice` takes for the
    identical exception.

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

    **Stage 7.9 task 5 (decision #154): the ledger is rebuilt from the
    FROZEN split, re-applied against what actually arrived.** `invoice_
    recipients(db, invoice.id)` reads back the `RecipientRule`s `issue_
    invoice` froze onto this invoice (every row whose `kind != "remainder"`,
    in `position` order — the remainder row itself is not a rule, it is
    `split_payment`'s own OUTPUT), and `ledger.split_payment` re-runs them
    against `transaction.amount` — never the snapshot's own `amount` column
    (that is the share OF THE INVOICE, display data) and never `invoice.
    amount` (a claim, possibly stale). The two amounts are EQUAL on the
    Payme path, where `CheckPerformTransaction`/`CreateTransaction` refuse a
    mismatch with `-31001`; this docstring claimed that as a general
    invariant until 2026-09-03, and the manual door deliberately breaks it.
    An accountant may confirm an UNDERPAYMENT — money that really arrived,
    less than was owed — and 3.10b files the difference as an open
    `reconciliations` row rather than refusing it. So a caller reading this
    must not assume `transaction.amount == invoice.amount`: re-running the
    frozen rules against the snapshot's own amounts here would allocate
    money that never arrived, silently, with every module's own suite
    staying green — reusing `invoice.amount` would repeat the identical
    mistake one level up.

    An invoice issued BEFORE this stage carries no snapshot at all —
    `invoice_recipients` returns `[]` — which `split_payment` already reads
    as "everything to the leshoz" (Task 2), the correct reading of "nobody
    is configured" and not an error: refusing those would strand every
    invoice already pending when this stage ships.

    `accounts = {None: leshoz_account}` only: a `payment_recipients` row
    identifies a Payme WALLET, not a bank account, so a configured
    receiver's `allocations.account` always stays `None` — the same
    reasoning `resolve_recipient_account`'s own docstring carries for the
    leshoz half this replaces (a placeholder string in a financial ledger is
    worse than `NULL`).

    **Raises `err("ERR-VAL-001", details={"reason": "split_does_not_fit"})`
    when the frozen rules do not fit `transaction.amount`** (review round 1,
    Important 1). `backoffice_schemas.ManualConfirmationIn.amount` is bounded
    only `gt=0` — an accountant may confirm LESS than the invoice, the whole
    point of the underpayment paragraph above — so a configured FIXED-amount
    receiver larger than what arrived makes `ledger.split_payment` raise
    `SplitDoesNotFit`. This mirrors `issue_invoice`'s own handling of the
    identical exception. Unreachable on the Payme path: `-31001` already
    pins `transaction.amount == invoice.amount`, and `issue_invoice` already
    proved the frozen rules fit the FULL invoice at issuance time.

    REFUSING is the right behaviour, not allocating a partial split: money
    that physically arrived is not lost by declining to mark the invoice
    `paid` — 3.10b's discrepancy register is exactly where an underpayment
    nobody can allocate belongs, and it can be confirmed the moment either
    the amount or the directory is corrected. The alternative — allocating
    anyway — would put a NEGATIVE row in a financial ledger, which is worse
    than an unconfirmed payment and far harder to notice. The check below
    runs BEFORE `invoice.status`/`.paid_at` are touched, so this leaves
    NOTHING written: no allocations, and the invoice stays exactly as it
    was — `get_db` rolls the caller's whole transaction back on this raise
    (decision #37), so no explicit rollback belongs here.
    """
    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        # invoice.application_id is a NOT NULL FK — unreachable in practice;
        # guarded rather than crashing into `None.contour_id` below. Caught
        # by payme_router.py's generic handler and answered -32400.
        raise err("ERR-SYS-003", details={"invoice": str(invoice.id)})

    snapshot = await invoice_recipients(db, invoice.id)
    rules = [
        ledger.RecipientRule(row.recipient_id, row.kind, row.percent, row.fixed_amount)
        for row in snapshot
        if row.kind != SNAPSHOT_KIND_REMAINDER
    ]
    try:
        shares = ledger.split_payment(transaction.amount, rules)
    except ledger.SplitDoesNotFit as exc:
        # Reached only through the manual maker-checker door (see the
        # docstring paragraph above) — refuse before any write, mirroring
        # `issue_invoice`'s own handling of the identical exception.
        raise err(
            "ERR-VAL-001",
            details={"reason": "split_does_not_fit", "detail": str(exc)},
        ) from exc

    invoice.status = "paid"
    invoice.paid_at = transaction.performed_at

    leshoz_account = await resolve_recipient_account(
        db, contour_id=application.contour_id, assigned_org_id=application.assigned_org_id
    )
    accounts: dict[uuid.UUID | None, str | None] = {None: leshoz_account}
    entries = ledger.entries_for_shares(
        invoice=invoice, transaction=transaction, shares=shares, accounts=accounts
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
       reversal is new rows rather than an edit of the old ones. `target`,
       `recipient_id` AND `account` are copied verbatim from the row being
       reversed — only `entry_type` and the sign of `amount` change — because
       a correction that drops `recipient_id` still matches `target=
       'receiver'` but no longer matches any ONE receiver: `dashboard.repo`'s
       per-receiver sum filters on `recipient_id`, not `target`, and a
       correction missing it would leave that receiver's reported share
       overstated by exactly the money that went back. This transaction's own
       entries always cancel out; the INVOICE's whole ledger sums to `0.00`
       too, as long as only one transaction ever performed against it, which
       is all today's paths allow (see the comment on `paid_rows` below);
    2. one `reconciliations` row, `result='discrepancy'`, `status='open'` —
       the register a human reads every morning, and the operator's handle on
       a case only a human can finish;
    3. an audit row carrying **RI-01** (`tz/10`: «PAID без подтверждения
       провайдера/банка» — the provider has withdrawn its confirmation and
       the invoice is still `paid`), plus a second one carrying **RI-10**
       («Разрешение активировано без оплаты», critical) when a permit already
       exists for this application in a LIVE status (`repo.
       permit_organization_for_application` — a `revoked` or `expired` permit
       does not raise it). Ruling #112: when RI-10 fires, `notify()` also
       tells the permit's own `executor_head` (`permits.manage`'s holder) —
       raising the indicator only, with nobody told, would leave a human
       decision waiting on somebody happening to read the register.
       Automatic suspension stays OUT of scope on purpose: a provider-side
       glitch would then switch off an honest holder's permit with nobody in
       the loop, which is the ruling's own reasoning, verbatim.

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
    if any(row.entry_type == ALLOCATION_ENTRY_CORRECTION for row in mine):
        logger.warning(
            "payments.reversal_already_recorded",
            invoice_id=str(invoice.id),
            transaction_id=str(transaction.id),
        )
        return

    # The rows THIS transaction wrote, not every `payment` row on the invoice:
    # only this transaction's money came back, and negating another
    # transaction's entries would put the ledger further from the truth, not
    # closer.
    #
    # On every path that exists today the two sets are identical, so the
    # invoice's WHOLE ledger sums back to `0.00` — but that is a consequence,
    # not the rule this code follows. It holds because an invoice is only ever
    # performed once: `payme._perform_transaction` refuses an invoice that is
    # not `pending`, and a reversal leaves it `paid` (ruling 15). Should a
    # second performing transaction ever become reachable, this function stays
    # correct and the whole-invoice sum stops being zero — that is the right
    # way round, and the test asserts the sum for the one-transaction case it
    # builds, not as a universal law.
    paid_rows = [row for row in mine if row.entry_type == ALLOCATION_ENTRY_PAYMENT]
    note = f"reversal of {transaction.provider} transaction {transaction.external_id}"
    if reason is not None:
        note = f"{note} (cancel reason {reason})"
    await repo.add_allocations(
        db,
        [
            Allocation(
                invoice_id=invoice.id,
                transaction_id=transaction.id,
                entry_type=ALLOCATION_ENTRY_CORRECTION,
                target=row.target,
                recipient_id=row.recipient_id,
                account=row.account,
                amount=-row.amount,
                note=note,
            )
            for row in paid_rows
        ],
    )

    reversed_amount = sum((row.amount for row in paid_rows), Decimal("0.00"))
    # **`reconciliations.difference` carries TWO sign conventions, and this is
    # the second one** (fix round 1). Everywhere a payment is compared with an
    # invoice — `matcher.py`, `statement_service`, `backoffice_service`'s
    # underpaid manual confirmation — it is paid MINUS invoiced, so an
    # underpayment is negative and an overpayment positive, the same sign a
    # bank line gets. There is no such comparison here: nothing was
    # under- or over-paid, and there is no bank line at all. What this row
    # reports is the money that WENT BACK, stored positive, and the row's own
    # `comment` says "reversed a confirmed payment of ..." so a register reader
    # is never left inferring it from the number. `transaction_id IS NOT NULL`
    # with `statement_line_id IS NULL` is what distinguishes the two kinds of
    # row in a query.
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

    # A LIVE permit for this application means money has gone back from
    # something already issued — `tz/10`'s RI-10 verbatim. A read-only SELECT,
    # never a call into `permits` (both modules are level 4): see
    # `repo.permit_organization_for_application`'s own comment for the
    # boundary argument, for which statuses count (a `revoked` or `expired`
    # permit does NOT — an RI-10 on either is a false positive on a CRITICAL
    # indicator), and for what drops if the trade is ever re-decided.
    permit_organization_id = await repo.permit_organization_for_application(
        db, invoice.application_id
    )
    if permit_organization_id is not None:
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

        # Ruling #112: raising RI-10 tells the prosecutor eventually (via
        # `oversight.sweep`'s harvest, on its own schedule); it tells nobody
        # at the leshoz AT ALL. `permits.manage`'s holder — `executor_head`,
        # who alone may suspend/resume/revoke — is who has to decide, so they
        # are who is notified, directly, in this same transaction.
        recipients = await auth_service.user_ids_with_permission(
            db, _PERMIT_DECISION_PERMISSION, organization_id=permit_organization_id
        )
        if not recipients:
            # Fails closed on the SIDE EFFECT, not on the record: an
            # organization with nobody holding `permits.manage` is a data
            # problem this function cannot fix, and the RI-10 row above
            # still stands for the prosecutor to find.
            logger.warning(
                "payments.reversal_notify_no_recipient",
                invoice_id=str(invoice.id),
                organization_id=str(permit_organization_id),
            )
        for recipient_id in recipients:
            await notifications_service.notify(
                db,
                event_code=events.PAYMENT_REVERSED,
                recipient_user_id=recipient_id,
                params={
                    "invoice_number": invoice.number,
                    "reversed_amount": reversed_amount,
                    "reason": reason,
                },
                object_type="invoice",
                object_id=invoice.id,
            )
