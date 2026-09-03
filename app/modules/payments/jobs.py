"""The daily sweep that closes an unpaid invoice's 10-day window (plan
`03.10a-payments-core` task 6, ruling 13), and 3.10b task 10's own daily
sweep, `refund_sla_sweep`, at the bottom of this file — the refund SLA's
own RI-07, «tz/10»'s medium-severity, daily-digest indicator for a
`requested`/`in_review` refund past its 20-working-day control deadline
(`refunds.due_at`). It shares this file for the same reason both of
`expiry_sweep`'s passes do: one `AsyncSession`, `app/workers/jobs.py`'s
thin session-opening wrapper, and `user_id=None` with ONE correlation id
per run. It needs neither of `expiry_sweep`'s two disciplines in full,
though, and says why at its own definition: RI-07 takes no row lock
(nothing on `refunds` is written, only `audit_log`) and runs ONE pass, not
two — there is no reminder half to a control deadline.

`expiry_sweep(db)` is pure business logic over an already-open `AsyncSession`
— `app/workers/jobs.py::expire_invoices` is the thin wrapper that opens the
session from an `async_sessionmaker`, calls this, and commits (mirrors
`process_gis_imports`'s split from `gis.import_service.process_pending`).

Two independent passes over `invoices.status = 'pending'`, both idempotent by
construction because each candidate set is defined by a status/date filter
that a successful pass removes the row from:

1. **Overdue** (`due_at` already in the past) — flips the invoice to
   `expired` and the application to `EXPIRED_UNPAID` through
   `applications.service.set_status`. It takes the **invoice lock first**
   and the application lock second (see "Lock order" below), re-reads the
   invoice under that lock, and skips a row that stopped being `pending`
   between the unlocked scan and the lock — a concurrent
   `PerformTransaction` that just paid it, say. Both writes carry the SAME
   job correlation id and `user_id=None` (`CLAUDE.md`'s job-audit idiom).
2. **Reminder** (`due_at` within `REMINDER_DAYS_BEFORE_DUE` days from now,
   not yet due) — notifies the applicant once. `notifications.service.
   already_notified` is the once-only check (no new column, no schema
   change beyond migration `0018`'s seed): looked up BEFORE calling
   `notify()`, keyed on an existing `inapp` row for the same
   `event_code`/`object_id`. This pass takes no lock at all — it writes
   nothing on `invoices` or `applications`.

Re-running the whole sweep is a no-op the second time: an expired invoice no
longer matches pass 1's `status = 'pending'` filter, and a reminder already
sent no longer passes `already_notified`.

--- Lock order: CONFIRMATION, then invoice, then application ---------------

**This section is the codebase's one registry of that rule. A new writer
that takes more than one of these row locks belongs here.**

`payme._perform_transaction` locks the **invoice** (`repo.get_invoice_for_
update`) and then, through `service.confirm_payment`, the **application**
(`applications.service.set_status` locks its own row). Pass 1 originally did
the opposite — `set_status` first, the invoice at flush — and an overdue
invoice being paid at the exact moment the sweep reached it was a textbook
ABBA deadlock. Whichever side Postgres picks to abort raises
`DeadlockDetected`, which is a `DBAPIError` and NOT a `DomainError`, so it
would have escaped the per-row guard, escaped `expire_invoices`, and left
the whole night's work uncommitted.

One consistent order across the codebase is the cure, not a wider `except`:
every writer here takes the invoice before the application (`issue_invoice`
inserts its invoice before calling `set_status`; `confirm_payment` is called
with the invoice already locked). Adding a new writer that touches both
means taking them in this order.

3.10b task 7 added a THIRD row lock ahead of both.
`backoffice_service.check_manual_confirmation` locks the
**manual_payment_confirmations** row (`repo.get_manual_confirmation_for_
update`), then the invoice, then — through `confirm_payment` — the
application. So the full order is:

    manual_payment_confirmations -> invoices -> applications

and no cycle is possible, because nothing that holds an invoice or
application lock ever reaches back for a confirmation row: the only other
writer of that table, `backoffice_service.file_manual_confirmation`, takes
**no lock at all** (it inserts, and its "one `pending_check` at a time"
read is unlocked — a deliberate, recorded deferral: two simultaneous
filings are register noise, never money, and the real fix is a partial
unique index costing this stage a second migration).

--- One savepoint per row --------------------------------------------------

Ruling 13's "one malformed row must never abort the whole nightly run" is an
outcome for the WHOLE sweep — `app/workers/jobs.py::expire_invoices` commits
ONCE, at the very end, so an uncaught exception anywhere in either pass rolls
back every write the sweep has already made.

Catching broadly is only half of that. A failure at the DATABASE level (the
deadlock above, an `IntegrityError`, a statement error) leaves the session in
`InFailedSQLTransaction`: every later statement fails too, and `COMMIT` on an
aborted PostgreSQL transaction is silently treated as `ROLLBACK` — the sweep
would "complete", log nothing alarming, and quietly discard everything. So
each row's whole body runs inside its own `db.begin_nested()` SAVEPOINT, and
the guard around it catches `Exception` (repo lesson: "A failed DB statement
aborts the whole transaction — catch the right type, recover with a
SAVEPOINT"). A `ROLLBACK TO SAVEPOINT` is one of the few statements PostgreSQL
still accepts in an aborted transaction, which is exactly what makes the NEXT
row's writes possible. The two named exception types keep their own log lines
because their diagnostics differ, not because the width of the guard depends
on them; and each loop reads the ids it logs BEFORE entering its savepoint,
since a rolled-back savepoint expires the objects it restores and an `except`
branch must not be able to emit a query of its own.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.time import TASHKENT, business_today
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.notifications import service as notifications_service
from app.modules.payments import events, repo
from app.modules.payments.models import Invoice

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


async def _expire_one_invoice(
    db: AsyncSession, invoice_id: uuid.UUID, *, correlation_id: str
) -> bool:
    """One overdue invoice's whole body, run by the caller inside a SAVEPOINT.
    `True` when it was expired, `False` when it was skipped.

    Takes the INVOICE lock first (module docstring, "Lock order") and only
    then `set_status`, which takes the application's. The re-read under that
    lock is not ceremony: `list_invoices_past_due` is an unlocked scan, so a
    `PerformTransaction` may have paid — or an `APPLICATION_CANCELLED`
    subscriber cancelled — this very row in between, and expiring a paid
    invoice is the same damage `cancel_invoice_for_application` refuses to
    do. `get_invoice_for_update` uses `populate_existing`, so the status
    read here is the locked, current one, never a stale cached value.

    `set_status` before the invoice write, still: it can legitimately refuse
    the transition, and it raises before writing anything, so a refusal
    leaves nothing half-applied (ruling 13 forbids exactly that state)."""
    invoice = await repo.get_invoice_for_update(db, invoice_id)
    if invoice is None or invoice.status != "pending":
        return False

    await applications_service.set_status(db, invoice.application_id, to_status="EXPIRED_UNPAID")
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
    return True


async def _expire_overdue_invoices(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    count = 0
    for candidate in await repo.list_invoices_past_due(db, now=now):
        # Plain strings, read BEFORE the savepoint. A savepoint rollback
        # expires the objects it restores, so reading `candidate.id` in an
        # `except` branch would emit a refresh SELECT from inside the error
        # path — the one place that must not be able to fail.
        invoice_id, application_id = candidate.id, str(candidate.application_id)
        try:
            async with db.begin_nested():
                expired = await _expire_one_invoice(db, invoice_id, correlation_id=correlation_id)
        except DomainError as exc:
            # A transition this job has no business making — e.g. a row
            # already moved on some other path. Expected and tolerated
            # (ruling 13), logged with enough identity to chase.
            logger.error(
                "job.expiry_sweep.bad_transition",
                invoice_id=str(invoice_id),
                application_id=application_id,
                error_code=exc.code,
            )
            continue
        except Exception as exc:
            # Everything else, database-level failures included (module
            # docstring, "One savepoint per row"): the SAVEPOINT above has
            # already been rolled back by the time this runs, so the session
            # is usable and the next invoice really can be written.
            logger.error(
                "job.expiry_sweep.expire_failed",
                invoice_id=str(invoice_id),
                application_id=application_id,
                error=repr(exc),
            )
            continue
        count += int(expired)
    return count


async def _remind_about_one_invoice(
    db: AsyncSession, invoice: Invoice, *, correlation_id: str
) -> bool:
    """One due-soon invoice's whole body, run by the caller inside a
    SAVEPOINT — the once-only check and the application read included, since
    either can fail at the database level just as `notify()` can. `True` when
    a reminder was sent, `False` when it was skipped.

    Takes the invoice object rather than an id, and no lock: this pass writes
    nothing on `invoices` or `applications`, so it is not part of the lock
    order pass 1's own helper has to respect."""
    if await notifications_service.already_notified(
        db, event_code=events.INVOICE_DUE_SOON, object_id=invoice.id
    ):
        return False

    application = await applications_service.get(db, invoice.application_id)
    if application is None:
        # invoice.application_id is a NOT NULL FK — unreachable in
        # practice; guarded rather than crashing into None below.
        logger.error("job.expiry_sweep.application_missing", invoice_id=str(invoice.id))
        return False

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
    return True


async def _send_due_soon_reminders(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    horizon = now + timedelta(days=REMINDER_DAYS_BEFORE_DUE)
    count = 0
    for candidate in await repo.list_invoices_due_soon(db, now=now, before=horizon):
        # Read before the savepoint, same reasoning as pass 1's.
        invoice_id, application_id = str(candidate.id), str(candidate.application_id)
        try:
            async with db.begin_nested():
                reminded = await _remind_about_one_invoice(
                    db, candidate, correlation_id=correlation_id
                )
        except ValueError as exc:
            # `notify()`'s own documented failure — an unresolvable
            # recipient or an unknown channel. Kept apart from the catch-all
            # below only for its own log line (lesson: log repr(e), not
            # f"{e}" — an empty str() on some exception types is
            # undiagnosable later).
            logger.error(
                "job.expiry_sweep.reminder_failed",
                invoice_id=invoice_id,
                application_id=application_id,
                error=repr(exc),
            )
            continue
        except Exception as exc:
            # Everything else — above all a database-level failure inside
            # `notify()`'s own `db.flush()`, which raises `IntegrityError`/
            # `DBAPIError` and would otherwise poison the session for the
            # rest of the sweep AND silently discard pass 1's whole night's
            # work at the single commit (module docstring).
            logger.error(
                "job.expiry_sweep.reminder_error",
                invoice_id=invoice_id,
                application_id=application_id,
                error=repr(exc),
            )
            continue
        count += int(reminded)
    return count


# --- 3.10b task 10: the refund SLA sweep (RI-07) -----------------------------

# `tz/10`'s indicator for a refund still `requested`/`in_review` past its own
# 20-working-day control deadline (`refunds.due_at`, `core.time.
# add_working_days` — ruling 18). Medium severity, daily digest: unlike
# RI-01/RI-10 this is not written against a "success" action that already
# happened — it is the whole point of this job's ONE write, so it carries no
# `result=` override of its own (the default, "success", is right: raising
# the indicator is the correct, legal outcome of finding an overdue refund,
# not a denial of anything).
REFUND_SLA_BREACH = "refund.sla_breach"
RISK_INDICATOR_REFUND_OVERDUE = "RI-07"


async def _flag_one_overdue_refund(
    db: AsyncSession, refund_id: uuid.UUID, *, correlation_id: str
) -> bool:
    """One overdue refund's whole body, run by the caller inside a
    SAVEPOINT — the once-only check included, since it can fail at the
    database level just as the audit insert can. `True` when RI-07 was
    raised, `False` when a previous run already raised it for this refund.

    Takes no lock: unlike EITHER of `expiry_sweep`'s two passes, this pass
    writes nothing on `refunds` itself — only an `audit_log` row — so there
    is no row to lock and no lock-order question at all (the same reasoning
    `_remind_about_one_invoice` gives for taking none). The once-only check
    is `audit.service.already_logged`, the shape `expiry_sweep`'s reminder
    pass uses with `notifications.service.already_notified` — RI-07 sends no
    notification, so that function does not apply here, and this one exists
    for exactly this caller. `object_type="refund"` is passed through (fix
    round 1) so the check is served by `ix_audit_log_object`
    (`object_type`, `object_id`, `occurred_at`) as an index scan rather than
    a `Seq Scan on audit_log` — `EXPLAIN`-confirmed, see `audit.repo.exists`'s
    own docstring."""
    if await audit.already_logged(
        db, action=REFUND_SLA_BREACH, object_type="refund", object_id=refund_id
    ):
        return False
    await audit.log(
        db,
        action=REFUND_SLA_BREACH,
        user_id=None,
        object_type="refund",
        object_id=refund_id,
        correlation_id=correlation_id,
        extra={"risk_indicator": RISK_INDICATOR_REFUND_OVERDUE},
    )
    return True


async def _flag_overdue_refunds(db: AsyncSession, *, today: date, correlation_id: str) -> int:
    count = 0
    for candidate in await repo.list_refunds_past_due(db, on_date=today):
        # Read before the savepoint, same reasoning as `expiry_sweep`'s own
        # two loops: a rolled-back savepoint expires the objects it
        # restores, so reading `candidate.id` from an `except` branch would
        # emit a refresh SELECT from inside the error path.
        refund_id = candidate.id
        try:
            async with db.begin_nested():
                flagged = await _flag_one_overdue_refund(
                    db, refund_id, correlation_id=correlation_id
                )
        except Exception as exc:
            # Broad on purpose (module docstring, "One savepoint per row"):
            # a database-level failure here must not poison the session for
            # the rest of the run, nor silently discard every refund this
            # sweep already flagged at the single commit
            # `app/workers/jobs.py::refund_sla_sweep` makes.
            logger.error(
                "job.refund_sla_sweep.flag_failed",
                refund_id=str(refund_id),
                error=repr(exc),
            )
            continue
        count += int(flagged)
    return count


async def refund_sla_sweep(db: AsyncSession) -> dict[str, int]:
    """Run the one pass once, in the caller's transaction. Returns a count
    for the caller (`app/workers/jobs.py::refund_sla_sweep`) to log.

    Only `requested`/`in_review` refunds past `due_at` are candidates
    (`repo.list_refunds_past_due`) — a `returned`/`rejected` one is terminal
    and untouched, however long past its own deadline: the money question is
    already settled, and RI-07 exists to flag an UNSETTLED one, not to
    relitigate a closed one."""
    today = business_today()
    correlation_id = f"job:{uuid.uuid4()}"
    flagged = await _flag_overdue_refunds(db, today=today, correlation_id=correlation_id)
    return {"flagged": flagged}
