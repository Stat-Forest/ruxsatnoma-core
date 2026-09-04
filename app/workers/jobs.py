"""Periodic jobs (decision #36). Each takes a session factory, opens its own
session, audits with user_id=None, and commits. Failures raise to the caller
(the scheduler wrapper logs and swallows)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import settings_store
from app.core.models import IdempotencyKey
from app.core.time import business_today
from app.modules.applications import jobs as applications_jobs
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.auth.models import OtpCode, Representation, Session
from app.modules.gis import import_service as gis_import_service
from app.modules.integrations.models import OutboxMessage
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import Notification
from app.modules.payments import jobs as payments_jobs
from app.modules.payments import statement_service as payments_statement_service
from app.modules.permits import jobs as permits_jobs

logger = structlog.get_logger(__name__)


async def purge_stale_rows(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Delete ephemeral operational rows past their retention (plan 03.4 ruling 11).

    The one sanctioned recurring hard delete: security artifacts, not business
    records — the business trail lives in audit_log/integration_log."""
    counts: dict[str, int] = {}
    async with factory() as db:
        now = datetime.now(UTC)
        otp_days = await settings_store.get_int(db, "purge_otp_after_days")
        sess_days = await settings_store.get_int(db, "purge_sessions_after_days")
        outbox_days = await settings_store.get_int(db, "purge_outbox_delivered_after_days")
        idem_hours = await settings_store.get_int(db, "purge_idempotency_after_hours")

        # .rowcount is a real int at runtime for a Core DELETE executed through
        # AsyncSession (asyncpg's CursorResult) — the async stubs just type
        # execute()'s return as the base Result, which doesn't declare it.
        result = await db.execute(
            delete(OtpCode).where(OtpCode.expires_at < now - timedelta(days=otp_days))
        )
        counts["otp_codes"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(Session).where(
                or_(Session.revoked_at.is_not(None), Session.expires_at < now),
                Session.expires_at < now - timedelta(days=sess_days),
            )
        )
        counts["sessions"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(OutboxMessage).where(
                OutboxMessage.status == "delivered",
                OutboxMessage.delivered_at < now - timedelta(days=outbox_days),
            )
        )
        counts["outbox_messages"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        result = await db.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.created_at < now - timedelta(hours=idem_hours)
            )
        )
        counts["idempotency_keys"] = result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        await audit.log(
            db,
            action="purge.run",
            user_id=None,
            correlation_id=f"job:{uuid.uuid4()}",
            extra=counts,
        )
        await db.commit()
    logger.info("job.purge_stale_rows", **counts)
    return counts


async def expire_representations(factory: async_sessionmaker[AsyncSession]) -> int:
    """Flip past-valid_until active representations to 'expired' (3.2b ruling 14
    carry-over): reads already exclude them; this makes the stored status true."""
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(Representation).where(
                        Representation.status == "active",
                        Representation.valid_until.is_not(None),
                        Representation.valid_until < business_today(),
                    )
                )
            )
            .scalars()
            .all()
        )
        correlation = f"job:{uuid.uuid4()}"
        for rep in rows:
            rep.status = "expired"
            await audit.log(
                db,
                action="representation.expire",
                user_id=None,
                object_type="representation",
                object_id=rep.id,
                correlation_id=correlation,
            )
        await db.commit()
    logger.info("job.expire_representations", expired=len(rows))
    return len(rows)


# The audience of system alerts. Kept as a code constant, not a setting: an
# administrator who could edit it could also silence the alert about the queue
# that stopped telling them anything.
ADMIN_ROLE_CODES = ("sys_admin", "central_admin")


async def alert_dead_outbox(factory: async_sessionmaker[AsyncSession]) -> int:
    """Report dead outbox rows to administrators in-app (plan 03.5 ruling 12).

    `integrations` is level 0 and cannot call `notifications` (level 2), so the
    alert cannot live inside `deliver_one`; this job is the seam. `alerted_at`
    makes it exactly-once, and any notification whose transport died is failed
    here — nothing will deliver it now."""
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.status == "dead", OutboxMessage.alerted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0
        now = datetime.now(UTC)
        by_destination: dict[str, int] = {}
        for row in rows:
            by_destination[row.destination] = by_destination.get(row.destination, 0) + 1
            row.alerted_at = now
        await db.execute(
            update(Notification)
            .where(
                Notification.outbox_message_id.in_([row.id for row in rows]),
                Notification.status.in_(("queued", "sent")),
            )
            .values(status="failed", error="transport gave up (outbox dead)")
        )
        correlation = f"job:{uuid.uuid4()}"
        admin_ids = await auth_service.list_user_ids_by_role_codes(db, ADMIN_ROLE_CODES)
        if not admin_ids:
            # This job exists to make a silent failure noisy. With nobody to alert we
            # would still stamp `alerted_at` and fail the notifications below, so the
            # rows end up marked "alerted" with no human ever told — the exact silence
            # the job was written to break. The log line is the fallback channel.
            logger.error(
                "job.alert_dead_outbox_no_admins",
                dead_rows=len(rows),
                by_destination=by_destination,
                roles=list(ADMIN_ROLE_CODES),
            )
        for destination, count in sorted(by_destination.items()):
            for user_id in admin_ids:
                await notifications_service.notify(
                    db,
                    event_code="system.outbox_dead",
                    recipient_user_id=user_id,
                    params={"destination": destination, "count": count},
                    channels=("inapp",),  # never send an SMS about SMS being broken
                    correlation_id=correlation,
                )
        await audit.log(
            db,
            action="outbox.alert_dead",
            user_id=None,
            correlation_id=correlation,
            extra=by_destination,
        )
        await db.commit()
    logger.info("job.alert_dead_outbox", **by_destination)
    return len(rows)


async def process_gis_imports(factory: async_sessionmaker[AsyncSession]) -> int:
    """Drain the geodata import queue (plan 03.6a ruling 6).

    Deliberately NOT the outbox: the outbox carries messages LEAVING the system,
    this is inbound work. It shares the outbox's claim idiom (FOR UPDATE SKIP
    LOCKED) so any number of workers can drain the queue side by side.

    One row per tick, not a loop: the scheduler runs this every 10 seconds with
    `coalesce=True`, so a backlog drains steadily while a single 151-feature
    parse can never hold the scheduler's thread for minutes at a time.
    `import_service` handles its own audit and its own failure records — a
    return of 0 simply means the queue was empty.
    """
    processed = await gis_import_service.process_pending(factory)
    if processed:
        logger.info("job.process_gis_imports", processed=processed)
    return processed


async def process_bank_statements(factory: async_sessionmaker[AsyncSession]) -> int:
    """Parse and match at most one queued bank statement (plan 03.10b task 4).

    Thin wrapper only — `payments.statement_service.process_pending(db)` holds
    the claim, the state machine and its own failure records, the same split
    `process_gis_imports` has from `gis_import_service.process_pending` and
    `expire_invoices` has from `payments.jobs.expiry_sweep`. One statement per
    tick, not a loop: a backlog drains steadily while a single month-long file
    can never hold the scheduler's thread.
    """
    async with factory() as db:
        processed = await payments_statement_service.process_pending(db)
        await db.commit()
    if processed:
        logger.info("job.process_bank_statements", processed=processed)
    return processed


async def expire_invoices(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Close an unpaid invoice's 10-day window and remind the applicant before
    it closes (plan 03.10a-payments-core task 6, ruling 13).

    Thin wrapper only — `payments.jobs.expiry_sweep(db)` holds the actual
    logic (both DB passes, the audit trail, the reminder), the same split
    `process_gis_imports` above has from `gis_import_service.process_pending`.
    This function's own job is the one every other job in this file already
    does: open a session, run it, commit."""
    async with factory() as db:
        counts = await payments_jobs.expiry_sweep(db)
        await db.commit()
    if counts["expired"] or counts["reminded"]:
        logger.info("job.expire_invoices", **counts)
    return counts


async def refund_sla_sweep(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Flag a refund still `requested`/`in_review` past its 20-working-day
    control deadline — RI-07 (plan `03.10b-payments-reconciliation` task 10).

    Thin wrapper only — `payments.jobs.refund_sla_sweep(db)` holds the
    actual logic (the candidate query, the once-only check, the audit
    trail), the same split `expire_invoices` above has from
    `payments.jobs.expiry_sweep`. This function's own job is the one every
    other job in this file already does: open a session, run it, commit."""
    async with factory() as db:
        counts = await payments_jobs.refund_sla_sweep(db)
        await db.commit()
    if counts["flagged"]:
        logger.info("job.refund_sla_sweep", **counts)
    return counts


async def sla_sweep(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Remind an application's office before its SLA deadline and raise
    RI-07 once it passes (plan `03.9b-applications-review` task 2).

    Thin wrapper only — `applications.jobs.sla_sweep(db)` holds the actual
    logic (both candidate queries, the once-only checks, the notification
    and the audit trail), the same split `refund_sla_sweep` above has from
    `payments.jobs.refund_sla_sweep`. This function's own job is the one
    every other job in this file already does: open a session, run it,
    commit. Same name as the module-level function it wraps, deliberately —
    `refund_sla_sweep` above is the precedent."""
    async with factory() as db:
        counts = await applications_jobs.sla_sweep(db)
        await db.commit()
    if counts["reminded"] or counts["flagged"]:
        logger.info("job.applications_sla_sweep", **counts)
    return counts


async def _drain_batches(
    factory: async_sessionmaker[AsyncSession],
    sweep: Callable[[AsyncSession, uuid.UUID | None], Awaitable[permits_jobs.SweepBatch]],
    *,
    name: str,
) -> int:
    """Run one permits sweep to exhaustion, ONE TRANSACTION PER BATCH.

    The batch bounds the transaction, the loop keeps the day unbounded (review,
    Important 3): a batch commits before the next is read, so a season ending for
    a whole district is still swept in full while no single transaction holds
    thousands of `FOR UPDATE` locks. `after_id` is a keyset cursor over
    `permits.id`, so the next batch resumes exactly where this one stopped and no
    permit is visited twice — including the rows a batch skipped or whose own
    SAVEPOINT rolled back.

    A short batch — fewer candidates than the batch size — is the only stop
    condition, and the cursor advancing monotonically is what guarantees it is
    reached. `failed` rows are logged individually by the sweep and reported once
    more here at error level: `_wrap` only sees raised exceptions, and nothing
    here raises, so this line is the signal that a permit is stuck.
    """
    total = failed = 0
    after_id: uuid.UUID | None = None
    while True:
        async with factory() as db:
            batch = await sweep(db, after_id)
            await db.commit()
        total += batch.processed
        failed += batch.failed
        after_id = batch.last_id
        if batch.scanned < permits_jobs.BATCH_SIZE:
            break
    if failed:
        logger.error(f"job.{name}.rows_failed", failed=failed, processed=total)
    elif total:
        logger.info(f"job.{name}", processed=total)
    return total


async def expire_permits(factory: async_sessionmaker[AsyncSession]) -> int:
    """The nightly permit expiry (plan 03.11a task 7, ruling 16).

    A wrapper, like every job on this page: the decision lives in
    `permits.jobs.expire_permits`, which takes a session so a test can drive it
    without a scheduler, and this drains it batch by batch. A permit's status,
    its history row, the holder's notification and its audit entry always share
    one transaction.
    """
    return await _drain_batches(
        factory,
        lambda db, after_id: permits_jobs.expire_permits(db, after_id=after_id),
        name="expire_permits",
    )


async def close_finished_permits(factory: async_sessionmaker[AsyncSession]) -> int:
    """Close the application behind every permit that has finished (ruling 13).

    Scheduled AFTER `expire_permits` so a permit that ran out last night has its
    application closed the same night rather than the next one — the sweep is
    correct in either order, since it scans every finished permit, but the pair
    is what a holder sees as one overnight step.
    """
    return await _drain_batches(
        factory,
        lambda db, after_id: permits_jobs.close_finished(db, after_id=after_id),
        name="close_finished_permits",
    )
