"""The daily sweep that closes an unpaid invoice's 10-day window (plan
`03.10a-payments-core` task 6, ruling 13).

`expiry_sweep(db)` is pure business logic over an already-open `AsyncSession`
— `app/workers/jobs.py::expire_invoices` is the thin wrapper that opens the
session from an `async_sessionmaker`, calls this, and commits (mirrors
`process_gis_imports`'s split from `gis.import_service.process_pending`).

Two independent passes over `invoices.status = 'pending'`, both idempotent by
construction because each candidate set is defined by a status/date filter
that a successful pass removes the row from:

1. **Overdue** (`due_at` already in the past) — flips the invoice to
   `expired` and the application to `EXPIRED_UNPAID` through
   `applications.service.set_status`, in THAT order: `set_status` fetches
   and locks the application row and can refuse the transition (a row this
   job has no business touching — e.g. one already moved on some other
   path), so only once it succeeds does the invoice itself get written.
   Reversing the order would risk flushing `invoice.status = 'expired'`
   before the refusal, leaving exactly the half-applied state ruling 13
   forbids. Both writes carry the SAME job correlation id and
   `user_id=None` (`CLAUDE.md`'s job-audit idiom). A refusal is caught,
   logged with enough identity to chase, and the sweep moves on to the next
   invoice — one malformed row must never abort the whole nightly run
   (ruling): in this repo's shared, persistent test database a leftover
   committed overdue invoice from another test would otherwise fail every
   payments test that runs after it.
2. **Reminder** (`due_at` within `REMINDER_DAYS_BEFORE_DUE` days from now,
   not yet due) — notifies the applicant once. `notifications.service.
   already_notified` is the once-only check (no new column, no schema
   change beyond migration `0018`'s seed): looked up BEFORE calling
   `notify()`, keyed on an existing `inapp` row for the same
   `event_code`/`object_id`.

Re-running the whole sweep is a no-op the second time: an expired invoice no
longer matches pass 1's `status = 'pending'` filter, and a reminder already
sent no longer passes `already_notified`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.time import TASHKENT
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.notifications import service as notifications_service
from app.modules.payments import events, repo

logger = structlog.get_logger(__name__)

# CLAUDE.md's audit invariant: action codes are "<object>.<verb>" in English,
# constant lives with the acting module — mirrors service.py's own
# INVOICE_ISSUE/INVOICE_CANCEL/INVOICE_PAY.
INVOICE_EXPIRE = "invoice.expire"

# Ops-tunable candidate (ruling), documented rather than wired to
# `system_settings`: the brief fixes no number (its own fixture is "due in
# two days") and `tz/08` gives none, so this is a deliberate, named default,
# not a value anyone has asked to make runtime-tunable yet.
REMINDER_DAYS_BEFORE_DUE = 3


async def expiry_sweep(db: AsyncSession) -> dict[str, int]:
    """Run both passes once, in the caller's transaction. Returns counts for
    the caller (`app/workers/jobs.py::expire_invoices`) to log."""
    now = datetime.now(UTC)
    correlation_id = f"job:{uuid.uuid4()}"

    expired = await _expire_overdue_invoices(db, now=now, correlation_id=correlation_id)
    reminded = await _send_due_soon_reminders(db, now=now, correlation_id=correlation_id)
    return {"expired": expired, "reminded": reminded}


async def _expire_overdue_invoices(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    count = 0
    for invoice in await repo.list_invoices_past_due(db, now=now):
        try:
            await applications_service.set_status(
                db, invoice.application_id, to_status="EXPIRED_UNPAID"
            )
        except DomainError as exc:
            # Never let one row's refusal abort the nightly run (ruling) —
            # logged with enough identity to chase (invoice/application ids,
            # the domain error's own code), and `invoice.status` is left
            # untouched: set_status raises before writing anything, so
            # nothing here needs undoing.
            logger.error(
                "job.expiry_sweep.bad_transition",
                invoice_id=str(invoice.id),
                application_id=str(invoice.application_id),
                error_code=exc.code,
            )
            continue
        invoice.status = "expired"
        await audit.log(
            db,
            action=INVOICE_EXPIRE,
            user_id=None,
            object_type="invoice",
            object_id=invoice.id,
            old_value={"status": "pending"},
            new_value={"status": "expired"},
            correlation_id=correlation_id,
        )
        count += 1
    return count


async def _send_due_soon_reminders(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    horizon = now + timedelta(days=REMINDER_DAYS_BEFORE_DUE)
    count = 0
    for invoice in await repo.list_invoices_due_soon(db, now=now, before=horizon):
        if await notifications_service.already_notified(
            db, event_code=events.INVOICE_DUE_SOON, object_id=invoice.id
        ):
            continue
        application = await applications_service.get(db, invoice.application_id)
        if application is None:
            # invoice.application_id is a NOT NULL FK — unreachable in
            # practice; guarded rather than crashing into None below.
            logger.error("job.expiry_sweep.application_missing", invoice_id=str(invoice.id))
            continue
        await notifications_service.notify(
            db,
            event_code=events.INVOICE_DUE_SOON,
            recipient_user_id=application.submitted_by_user_id,
            # Same three placeholder names the `invoice.issued` template
            # uses (migration 0009) and this stage's own `invoice.due_soon`
            # seed (migration 0018) — the reminder is the same fact
            # ("amount X due by date Y for application Z"), read again.
            params={
                "application_number": application.number,
                "amount": invoice.amount,
                "due_date": invoice.due_at.astimezone(TASHKENT).date(),
            },
            object_type="invoice",
            object_id=invoice.id,
            correlation_id=correlation_id,
        )
        count += 1
    return count
