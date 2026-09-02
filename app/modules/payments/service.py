"""Payments service — turning an approved application into an invoice, and
answering who may read it (design/02 § payments, plan `03.10a-payments-core`
task 2).

Public surface for the event bus (`subscribers.py`, registered in
`app/event_subscriptions.py`):

- `issue_invoice(db, application_id) -> Invoice` — the whole business action
  behind `applications.events.APPLICATION_APPROVED`: freeze the application's
  current calculation into an invoice, move the application to INVOICED, and
  notify the applicant. Idempotent (see its own docstring).
- `invoice_for_application(db, application_id) -> Invoice | None` — the
  in-force (`pending`/`paid`) invoice, or `None`. What `issue_invoice` checks
  first, and what a later module (3.11 permits: has this application been
  paid?) may safely call too — no permission or zone rule of its own, the
  caller is another SERVICE inside this process, mirroring
  `applications.service.get`/`norms.service.effective_norm`.
- `cancel_invoice_for_application(db, application_id) -> Invoice | None` — the
  business action behind `applications.events.APPLICATION_CANCELLED`.
  Idempotent and silent when there is no in-force invoice.
- `confirm_payment(db, *, invoice, transaction) -> None` (task 4, ruling J) —
  the whole business action behind a successful Payme `PerformTransaction`:
  invoice -> paid, the 50/50 ledger written, application -> PAID, the
  applicant notified, `payment_confirmed` published on the bus. Called from
  `payme.py` ONLY, already inside the caller's own transaction and already
  past every Payme-protocol check (idempotency, amount, payability) — this
  function performs no check of its own beyond resolving the recipient
  account.

`get_invoice_for_actor`/`list_invoices_for_actor` are this module's OWN
router-facing functions (they take an HTTP `actor: User`, unlike the
functions above) — not part of the cross-module public surface.
"""

import uuid
from datetime import UTC, datetime, timedelta

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
from app.modules.notifications import service as notifications_service
from app.modules.payments import events, ledger, repo
from app.modules.payments.models import Invoice, ProviderTransaction
from app.modules.payments.permissions import PAYMENTS_VIEW

# CLAUDE.md's audit invariant: action codes are "<object>.<verb>" in English,
# and the constant lives with the acting module (mirrors
# applications.service.APPLICATION_STATUS_CHANGE's own idiom).
INVOICE_ISSUE = "invoice.issue"
INVOICE_CANCEL = "invoice.cancel"
INVOICE_PAY = "invoice.pay"

# design/03 §"Public numbers": every invoice number starts with this prefix.
INVOICE_NUMBER_PREFIX = "INV"

# design/02: an invoice is payable for 10 calendar days from issuance.
DUE_PERIOD = timedelta(days=10)


async def invoice_for_application(db: AsyncSession, application_id: uuid.UUID) -> Invoice | None:
    """The in-force (`pending`/`paid`) invoice for `application_id`, or
    `None`. No permission or zone rule: the caller is another SERVICE inside
    this process, same shape as `applications.service.get`."""
    return await repo.get_in_force_invoice(db, application_id)


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
        raise err(
            "ERR-SYS-003",
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
    repeated event, must not raise (ruling 16, half 1)."""
    invoice = await invoice_for_application(db, application_id)
    if invoice is None:
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


async def _may_view_invoices_of(db: AsyncSession, applicant_id: uuid.UUID, *, actor: User) -> bool:
    """`payments.view` (or sys_admin) sees any invoice; otherwise the actor
    must OWN the same applicant identity the invoice's application belongs
    to — matched on `applicant_id`, never `submitted_by_user_id`: a legal
    entity has several representatives, and the one who happens to have
    submitted THIS particular application is not the only one entitled to
    see its invoice."""
    if await _holds_payments_view(db, actor):
        return True
    own_applicant = await auth_service.get_own_applicant(db, actor.id)
    return own_applicant is not None and own_applicant.id == applicant_id


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
    if not await _may_view_invoices_of(db, application.applicant_id, actor=actor):
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
    if not await _may_view_invoices_of(db, application.applicant_id, actor=actor):
        raise err("ERR-SYS-003", details={"application": str(application_id)})
    return await repo.list_invoices_by_application(db, application_id, limit=limit, offset=offset)


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
    """The whole business action behind a successful Payme `PerformTransaction`
    (`payme.py` task 4, ruling J) — invoice -> paid, the 50/50 ledger written,
    application -> PAID, the applicant notified, `payment_confirmed`
    published — all inside the CALLER's transaction: `payme.py` owns the
    session (via `payme_router.py`'s `get_db`), and this function neither
    commits nor is meant to be called from anywhere but a just-confirmed
    `PerformTransaction`, which has already run every Payme-protocol check
    (idempotency, amount, payability) this function does not repeat.

    Ruling I: the amount split is `transaction.amount` — the money that
    actually arrived — never `invoice.amount`. The two are equal by
    construction (`CheckPerformTransaction`/`CreateTransaction` refuse a
    mismatch with `-31001`), and Task 3's own tests already prove the
    sourcing rule this function relies on (`ledger.entries_for`).
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

    await publish(db, Event(name=events.PAYMENT_CONFIRMED, payload={"invoice_id": str(invoice.id)}))
