"""The nightly SLA sweep (plan `03.9b-applications-review` task 2). Mirrors
`payments.jobs.expiry_sweep`/`refund_sla_sweep` in shape: one `AsyncSession`
supplied by the caller (`app/workers/jobs.py`'s thin wrapper opens it and
commits once, at the end), one correlation id for the whole run, `user_id=
None` on the audit row this job writes (`CLAUDE.md`'s job-audit idiom).

Two independent passes, both idempotent by construction because each
candidate set is defined by a status/deadline filter that a successful pass
removes the row from:

1. **Reminder** — an SLA-active application (`sla.SLA_ACTIVE_STATUSES`) due
   within `SLA_REMINDER_DAYS_BEFORE` days, reminded to the ASSIGNED EXECUTOR
   and the head of the application's organization (`tz/04` scenario line 45:
   «приближение SLA -> напоминание исполнителю и руководителю») — never the
   applicant, who cannot make the office decide any faster.
   `notifications.service.already_notified` is the once-only check, one per
   RECIPIENT (its own `recipient_user_id` filter, fix round 1) — the same
   shape `payments.jobs._remind_about_one_invoice` uses per object.
2. **RI-07** — an SLA-active application whose deadline has already passed,
   not yet flagged. `audit.service.already_logged` is the once-only check,
   the same shape `payments.jobs._flag_one_overdue_refund` uses — a risk
   indicator is a fact in the journal, not a notification.

Neither pass takes a row lock: like `refund_sla_sweep`, this job writes
nothing on `applications` itself — only `notifications` and `audit_log` —
so there is no row to lock and no lock-order question at all.

`now = datetime.now(UTC)`, never `business_today()`: `applications.
sla_deadline_at` is a precise instant (`submitted_at + timedelta(days=
SLA_DAYS)`, service.py ruling 13), not a calendar date, the same reasoning
`payments.jobs.expiry_sweep` gives for its own `invoices.due_at` —
`business_today()` would throw away the time-of-day precision the deadline
was computed with. `payments.jobs.refund_sla_sweep`'s OWN use of
`business_today()` is not a counter-example: `refunds.due_at` there is a
plain `date` column.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import repo
from app.modules.applications.models import Application
from app.modules.applications.permissions import APPLICATIONS_DECIDE
from app.modules.applications.sla import is_overdue
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.notifications import service as notifications_service

logger = structlog.get_logger(__name__)

# `tz/10` §108 defines RI-07 as one indicator over two rules — 15 days here,
# 20 working days for `payments.jobs.RISK_INDICATOR_REFUND_OVERDUE` — so this
# is deliberately the SAME code as that constant, not a collision to avoid.
RISK_INDICATOR_SLA_OVERDUE = "RI-07"
# CLAUDE.md's audit invariant: action codes are "<object>.<verb>" in English,
# constant lives with the acting module — mirrors payments.jobs.INVOICE_EXPIRE
# and payments.jobs.REFUND_SLA_BREACH.
APPLICATION_SLA_BREACH = "application.sla_breach"
# DOTTED (module docstring, `applications/events.py`): this is what `notify()`
# looks a template up by, registered below in `applications.events.
# NOTIFIED_EVENT_CODES` and seeded by migration 0025.
NOTIFY_APPLICATION_SLA_APPROACHING = "application.sla_approaching"
# `tz/10` names no number; the brief's own fixture is "due in two days", so
# this is a deliberate, named default (mirrors payments.jobs.
# REMINDER_DAYS_BEFORE_DUE), not a value anyone has asked to make tunable yet.
SLA_REMINDER_DAYS_BEFORE = 3


async def _reminder_recipients(db: AsyncSession, application: Application) -> set[uuid.UUID]:
    """The ASSIGNED EXECUTOR and the head of the application's organization —
    `tz/04` scenario line 45 names both, never the applicant. The head is
    resolved through `APPLICATIONS_DECIDE` (`applications.permissions`'s own
    module docstring: this permission "belongs to `executor_head` and to it
    alone"), the SAME accessor `_auto_assign_on_submission` uses for
    `APPLICATIONS_REVIEW` — never a raw `role.code == "executor_head"`
    comparison, which would be a second, driftable source of the same rule.

    Either half can come back empty: an application somehow missing its
    active assignment (logged, matches `permits.subscribers.
    on_payment_confirmed`'s own "log and skip" shape for the identical
    column), or an organization with nobody currently holding the decide
    permission."""
    recipients: set[uuid.UUID] = set()
    if application.assigned_user_id is not None:
        recipients.add(application.assigned_user_id)
    else:
        logger.info("job.sla_sweep.reminder_unassigned", application_id=str(application.id))
    if application.assigned_org_id is not None:
        heads = await auth_service.user_ids_with_permission(
            db, APPLICATIONS_DECIDE, organization_id=application.assigned_org_id
        )
        recipients.update(heads)
    return recipients


async def _remind_one(db: AsyncSession, application: Application, *, correlation_id: str) -> bool:
    """One due-soon application's whole body, run by the caller inside a
    SAVEPOINT (mirrors `payments.jobs._remind_about_one_invoice`). `True`
    when at least one recipient was actually reminded, `False` when every
    resolved recipient already had one for THIS deadline (or none could be
    resolved at all).

    Each recipient carries its OWN `already_notified` check
    (`recipient_user_id=`): the executor's own notification row must not
    make the head look already-reminded, or vice versa.

    **Final whole-branch review, IMPORTANT.** The once-only key also carries
    the deadline itself now (`params_match`), not merely the event/object/
    recipient triple: `sla_deadline_at` moves forward every time a
    `PENDING_INFO` pause closes (`service.respond_info`, `sla.shift_deadline`),
    and without this a reminder sent against one deadline made every LATER
    deadline on the same application look already-warned-about, forever — the
    one notification the office got would go on naming a date a pause had
    already moved past. `notify()`'s own `params` already carries the
    deadline (below), so keying on its stored value needs no new column."""
    deadline = application.sla_deadline_at
    assert deadline is not None  # the candidate query's own WHERE clause
    deadline_key = deadline.date().isoformat()
    params = {"application_number": application.number, "deadline": deadline.date()}

    sent = False
    for recipient_id in await _reminder_recipients(db, application):
        if await notifications_service.already_notified(
            db,
            event_code=NOTIFY_APPLICATION_SLA_APPROACHING,
            object_id=application.id,
            recipient_user_id=recipient_id,
            params_match={"deadline": deadline_key},
        ):
            continue
        await notifications_service.notify(
            db,
            event_code=NOTIFY_APPLICATION_SLA_APPROACHING,
            recipient_user_id=recipient_id,
            params=params,
            object_type="application",
            object_id=application.id,
            correlation_id=correlation_id,
        )
        sent = True
    return sent


async def _send_reminders(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    horizon = now + timedelta(days=SLA_REMINDER_DAYS_BEFORE)
    count = 0
    for candidate in await repo.list_applications_sla_due_soon(db, now=now, before=horizon):
        # Read before the savepoint, same reasoning as `payments.jobs`'s own
        # loops: a rolled-back savepoint expires the objects it restores.
        application_id = candidate.id
        try:
            async with db.begin_nested():
                reminded = await _remind_one(db, candidate, correlation_id=correlation_id)
        except ValueError as exc:
            # `notify()`'s own documented failure — an unresolvable recipient
            # or an unknown channel. Kept apart from the catch-all below only
            # for its own log line (lesson: log repr(e), not f"{e}").
            logger.error(
                "job.sla_sweep.reminder_failed",
                application_id=str(application_id),
                error=repr(exc),
            )
            continue
        except Exception as exc:
            # Everything else, database-level failures included: the
            # SAVEPOINT above has already been rolled back by the time this
            # runs, so the session is usable and the next application really
            # can be written.
            logger.error(
                "job.sla_sweep.reminder_error",
                application_id=str(application_id),
                error=repr(exc),
            )
            continue
        count += int(reminded)
    return count


async def _flag_one(db: AsyncSession, application_id: uuid.UUID, *, correlation_id: str) -> bool:
    """One overdue application's whole body, run by the caller inside a
    SAVEPOINT (mirrors `payments.jobs._flag_one_overdue_refund`). `True` when
    RI-07 was raised, `False` when a previous run already raised it."""
    if await audit.already_logged(
        db, action=APPLICATION_SLA_BREACH, object_type="application", object_id=application_id
    ):
        return False
    await audit.log(
        db,
        action=APPLICATION_SLA_BREACH,
        user_id=None,
        object_type="application",
        object_id=application_id,
        correlation_id=correlation_id,
        extra={"risk_indicator": RISK_INDICATOR_SLA_OVERDUE},
    )
    return True


async def _flag_overdue(db: AsyncSession, *, now: datetime, correlation_id: str) -> int:
    count = 0
    for candidate in await repo.list_applications_past_sla_deadline(db, now=now):
        application_id = candidate.id
        deadline = candidate.sla_deadline_at
        assert deadline is not None  # the candidate query's own WHERE clause
        if not is_overdue(candidate.status, deadline, now):
            # A race between the unlocked scan above and this loop (the
            # application was decided or paused in between) — not this
            # pass's job to flag, matches `refund_sla_sweep`'s own
            # "untouched, however long past its own deadline" reasoning.
            continue
        try:
            async with db.begin_nested():
                flagged = await _flag_one(db, application_id, correlation_id=correlation_id)
        except Exception as exc:
            logger.error(
                "job.sla_sweep.flag_failed",
                application_id=str(application_id),
                error=repr(exc),
            )
            continue
        count += int(flagged)
    return count


async def sla_sweep(db: AsyncSession) -> dict[str, int]:
    """Run both passes once, in the caller's transaction (`app/workers/jobs.
    py::sla_sweep` opens the session and commits)."""
    now = datetime.now(UTC)
    correlation_id = f"job:{uuid.uuid4()}"
    reminded = await _send_reminders(db, now=now, correlation_id=correlation_id)
    flagged = await _flag_overdue(db, now=now, correlation_id=correlation_id)
    return {"reminded": reminded, "flagged": flagged}
