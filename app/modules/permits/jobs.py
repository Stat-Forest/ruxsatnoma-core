"""The two daily sweeps this module owns: a permit whose period has run out, and
the application a finished permit lets close.

Each call does ONE BATCH inside the caller's transaction and reports where it
stopped; `app/workers/jobs.py` holds the wrappers that open a session per batch,
commit it, and come back with the cursor advanced until the queue is drained.
They run on the APScheduler instance stage 3.4 already runs behind a PostgreSQL
advisory lock (ruling 16), never on a second mechanism of their own.

**Why a batch and not the whole night.** The expiry sweep holds a `FOR UPDATE`
lock on every candidate it read, through roughly seven statements per row —
application read, contact read, template read, notification insert, outbox
enqueue, audit, history — until the transaction commits. Unbounded, that is one
transaction holding every swept permit's row lock for the length of the sweep,
and a single raising row would roll back the whole night's work with only a log
line from `_wrap` (review, Important 3). Batching bounds the transaction without
bounding the DAY: the worker keeps going until a short batch says there is
nothing left, so a season that ends for a whole district in one night is still
fully swept. And each row runs inside its own SAVEPOINT, so one bad row costs
itself and not its batch — a plain rollback would poison the transaction for
every row after it (lesson: recovering from a failed statement to keep writing
on the same session needs a SAVEPOINT).

**Both are idempotent, and each by a different mechanism.** `expire_permits`
moves the permit out of the status its own candidate query selects on, so a
second run finds nothing; `close_finished` reads the application's status before
moving it, because its candidate set — every finished permit — does not shrink
when the work is done. Neither may act twice on one row: a second
`permit_status_history` entry would claim a transition that never happened, and a
second `set_status` would raise `ERR-APP-004` on the whole sweep.

**Lock order: the permit first, the application second, in both sweeps.**
`service.add_signature` locks the permit (`repo.permit_by_id_for_update`) and
then the application (`applications.service.set_status`), and that is the only
lock ordering anywhere in `app/`. `expire_permits` locks permits and no
application at all; `close_finished` locks no permit and only the application
`set_status` takes for itself. A sweep that read a permit under an application
lock would close the first real deadlock cycle in this codebase.
"""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import business_today
from app.modules.applications import service as applications_service
from app.modules.audit import service as audit
from app.modules.notifications import service as notifications
from app.modules.permits import events, repo, service
from app.modules.permits.models import ForestTicket, Permit, PermitStatusHistory

logger = structlog.get_logger(__name__)

EXPIRED_STATUS = "expired"
REVOKED_STATUS = "revoked"
# What "the permit is over" means, for the closure sweep. `archived` is NOT here:
# 4.7 owns `* -> ARCHIVED`, and an archived permit's application was closed on the
# way past this sweep long before.
FINISHED_STATUSES = (EXPIRED_STATUS, REVOKED_STATUS)

# tz/05's end state for an application whose permit has run its course. The other
# half of the pair `service.APPLICATION_PERMIT_ISSUED` opens; `PERMIT_ISSUED ->
# CLOSED` is the only edge out of that status in `APPLICATION_TRANSITIONS`.
APPLICATION_CLOSED = "CLOSED"

# How many permits one transaction may hold at a time. Big enough that an
# ordinary night is one or two batches, small enough that the `FOR UPDATE` locks
# of the expiry sweep are never held across thousands of rows' worth of work.
BATCH_SIZE = 200


@dataclass(frozen=True)
class SweepBatch:
    """What one batch did, and where the next one resumes.

    `scanned` is the candidate count, and a value below the batch's own `limit`
    is what tells the worker the queue is drained — `processed` cannot say that,
    because the closure sweep legitimately skips candidates whose application is
    already CLOSED. `failed` counts rows whose SAVEPOINT rolled back; they stay
    candidates and are retried on the next run.
    """

    scanned: int
    processed: int
    failed: int
    last_id: uuid.UUID | None


async def expire_permits(
    db: AsyncSession, *, limit: int = BATCH_SIZE, after_id: uuid.UUID | None = None
) -> SweepBatch:
    """Move up to `limit` permits whose period ended before today out of `active`.

    `business_today()` — Asia/Tashkent — and never `date.today()`, which follows
    the SERVER's zone and reports yesterday for roughly five hours a day on a UTC
    container (lesson). On this sweep that would expire a permit on its own last
    day, a day early, every night between 19:00 and midnight Tashkent.

    `period_to` is INCLUSIVE: the comparison is `period_to < today`, so a permit
    ending today is still in force today and expires tomorrow morning.

    `active` and `suspended` are both swept (Task 7, ruling 8):
    `pending_signatures` never came into force (C11), so «муддати тугаган» would
    be a false statement about it on the public check page — `tz/12` #16 is the
    open question for THAT status, and `watch_stalled_permits` below is what
    this stage does about it, deliberately short of a status change. `suspended`
    joins `active` because `PERMIT_TRANSITIONS` has always allowed `suspended ->
    expired`: without it a permit suspended mid-season would stay `suspended`
    forever once its period ran out, and its application would never reach
    `close_finished` either. `permits_ending_before` takes the candidates
    `FOR UPDATE`, so a permit revoked while this sweep waited drops out of the
    result instead of being expired on top of the revocation.

    One row's status change, its history row, the holder's notification and its
    audit entry share a SAVEPOINT: all four land or none of them do, so a permit
    can never be left expired with nobody told, and a row that raises rolls back
    alone instead of taking its batch with it.
    """
    today = business_today()
    rows = await repo.permits_ending_before(
        db, today, statuses=(service.ACTIVE_STATUS, "suspended"), limit=limit, after_id=after_id
    )
    correlation = f"job:{uuid.uuid4()}"
    # Read every id BEFORE any SAVEPOINT opens. Rolling one back restores the
    # session snapshot, which EXPIRES the instances modified inside it — so
    # `permit.id` on the way out of an `except` is a lazy reload from a
    # non-async context and raises `MissingGreenlet`, not the failure being
    # reported. The cursor at the end has the same trap.
    ids = [permit.id for permit in rows]
    processed = failed = 0
    for permit, permit_id in zip(rows, ids, strict=True):
        try:
            async with db.begin_nested():
                await _expire_one(db, permit, correlation=correlation)
        except Exception as exc:
            # `repr`, never f"{exc}": an asyncio-flavour TimeoutError has an EMPTY
            # str() and the line would read "expiry failed: " (lesson).
            failed += 1
            logger.error(
                "job.expire_permits.row_failed",
                permit_id=str(permit_id),
                error=repr(exc),
                exc_info=True,
            )
        else:
            processed += 1
    return SweepBatch(
        scanned=len(rows), processed=processed, failed=failed, last_id=ids[-1] if ids else None
    )


async def _expire_one(db: AsyncSession, permit: Permit, *, correlation: str) -> None:
    """One permit's whole expiry, inside the caller's SAVEPOINT."""
    # Captured BEFORE the write below. `active` and `suspended` both reach here
    # now (ruling 8) — the literal `service.ACTIVE_STATUS` this used to hard-code
    # would file `active -> expired` on a permit that was actually `suspended`, a
    # false statement on an append-only timeline that cannot be corrected
    # afterwards.
    from_status = permit.status
    permit.status = EXPIRED_STATUS
    # Flushes the status change with it, which is what makes a second run
    # inside this same transaction find nothing (the idempotency test runs
    # the sweep twice without committing in between).
    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=from_status,
            to_status=EXPIRED_STATUS,
            # No actor: `tz/05` and design/02 both make EXPIRED the job's,
            # not a person's. `changed_by` is nullable for exactly this.
            changed_by=None,
        ),
    )
    await notifications.notify(
        db,
        event_code=events.PERMIT_EXPIRED,
        recipient_user_id=await service._holder_recipient(db, permit),
        params={
            "permit_number": service._permit_number(permit.series, permit.number),
            **notifications.transition_params(from_status=from_status, to_status=EXPIRED_STATUS),
        },
        object_type=service.OBJECT_TYPE,
        object_id=permit.id,
        correlation_id=correlation,
    )
    await audit.log(
        db,
        action=service.PERMIT_EXPIRE,
        user_id=None,
        object_type=service.OBJECT_TYPE,
        object_id=permit.id,
        old_value={"status": from_status},
        new_value={"status": EXPIRED_STATUS},
        correlation_id=correlation,
    )


async def watch_stalled_permits(
    db: AsyncSession, *, limit: int = BATCH_SIZE, after_id: uuid.UUID | None = None
) -> SweepBatch:
    """Report — and ONLY report — a permit whose whole period elapsed unsigned.

    **This job moves nothing, and that is the design** (ruling 16). `tz/12` #16
    is open with the Agency: a paid permit whose recipient never signs can be
    neither revoked nor expired, its application never closes, and the citizen's
    money is frozen. 3.11a made the state honest; this makes it VISIBLE. It does
    not answer the question, because the answer changes what a signature means
    and no engineer may decide that.

    **Where the Agency's answer lands.** Whichever of `tz/12` #16's three
    options is chosen, it is (1) a new edge out of `pending_signatures` in
    `service.PERMIT_TRANSITIONS`, and (2) a `service.set_status(...)` call in
    this loop, beside the notification. Nothing else in this module changes.

    **The trap, so nobody takes the obvious shortcut instead.** Adding
    `pending_signatures -> revoked` so a head can cancel a mis-issued permit
    makes things WORSE: `permits.application_id` is UNIQUE, so the application
    would then be PAID with a revoked permit attached and no second permit ever
    issuable. Unwedging that way needs a re-issuance story, which needs the
    uniqueness relaxed, which is the invariant this module rests on.

    Once-only is a read before the write — `notifications.already_notified` —
    because unlike the expiry sweep, this job's candidate set does not shrink
    when the work is done. No assigned executor means nobody to tell: log and
    skip, the shape `subscribers.on_payment_confirmed` already uses.
    """
    today = business_today()
    rows = await repo.stalled_permits(db, today, limit=limit, after_id=after_id)
    correlation = f"job:{uuid.uuid4()}"
    # Read every id BEFORE any SAVEPOINT opens — see `expire_permits` for why
    # an id read after a rollback is a `MissingGreenlet`, not a value.
    ids = [permit.id for permit in rows]
    processed = failed = 0
    for permit, permit_id in zip(rows, ids, strict=True):
        if await notifications.already_notified(
            db, event_code=events.PERMIT_UNSIGNED_STALLED, object_id=permit_id
        ):
            continue
        application = await applications_service.get(db, permit.application_id)
        if application is None or application.assigned_user_id is None:
            logger.info("job.watch_stalled_permits.unassigned", permit_id=str(permit_id))
            continue
        try:
            # A SAVEPOINT, like every write in this file's other sweeps
            # (module docstring): `notify()` does an unflushed `db.add()`
            # before any statement runs, so a later failure inside it (an
            # outbox insert for a non-`inapp` channel, say) would otherwise
            # abort the WHOLE transaction — and the next row's own
            # `already_notified` SELECT would then raise
            # `InFailedSQLTransactionError` too, cascading through the rest
            # of the batch instead of costing only this row.
            async with db.begin_nested():
                await notifications.notify(
                    db,
                    event_code=events.PERMIT_UNSIGNED_STALLED,
                    recipient_user_id=application.assigned_user_id,
                    params={"permit_number": service._permit_number(permit.series, permit.number)},
                    object_type=service.OBJECT_TYPE,
                    object_id=permit_id,
                    correlation_id=correlation,
                )
        except Exception as exc:
            # `repr`, never f"{exc}" — see `expire_permits` for why.
            failed += 1
            logger.error(
                "job.watch_stalled_permits.row_failed",
                permit_id=str(permit_id),
                error=repr(exc),
                exc_info=True,
            )
        else:
            processed += 1
    return SweepBatch(
        scanned=len(rows), processed=processed, failed=failed, last_id=ids[-1] if ids else None
    )


async def close_finished(
    db: AsyncSession, *, limit: int = BATCH_SIZE, after_id: uuid.UUID | None = None
) -> SweepBatch:
    """Close the application of every permit in this batch that has finished
    (ruling 13).

    It moves the APPLICATION, not the permit: `PERMIT_ISSUED -> CLOSED` through
    `applications.service.set_status`, the one way a level-4 module moves an
    application. `* -> ARCHIVED` is 4.7's and this stage does not anticipate it.

    The candidate set does not shrink as the work is done — a closed application
    leaves its permit exactly where it was — so idempotence is a READ: the
    application's own status decides, and anything not still in `PERMIT_ISSUED`
    is skipped. Calling `set_status` unconditionally would instead raise
    `ERR-APP-004` (a transition to the status it already holds is not in
    `APPLICATION_TRANSITIONS`) on every already-closed row.

    That read is one `db.get` per candidate, and the candidate set grows until
    4.7 starts archiving. The alternative — one statement joining `permits` to
    `applications` — is not open to this module: `permits` reaches `applications`
    only through its service (design/01 rule 3, CLAUDE.md), and widening that
    service's frozen public surface is 3.9b's call, not this task's.
    """
    rows = await repo.permits_in_statuses(db, FINISHED_STATUSES, limit=limit, after_id=after_id)
    correlation = f"job:{uuid.uuid4()}"
    # Before any SAVEPOINT opens — see `expire_permits` for why an id read after
    # a rollback is a `MissingGreenlet` rather than a value.
    ids = [permit.id for permit in rows]
    processed = failed = 0
    for permit, permit_id in zip(rows, ids, strict=True):
        application = await applications_service.get(db, permit.application_id)
        if application is None or application.status != service.APPLICATION_PERMIT_ISSUED:
            continue
        try:
            async with db.begin_nested():
                await _close_one(db, permit, correlation=correlation)
        except Exception as exc:
            failed += 1
            logger.error(
                "job.close_finished.row_failed",
                permit_id=str(permit_id),
                error=repr(exc),
                exc_info=True,
            )
        else:
            processed += 1
    return SweepBatch(
        scanned=len(rows), processed=processed, failed=failed, last_id=ids[-1] if ids else None
    )


async def _close_one(db: AsyncSession, permit: Permit, *, correlation: str) -> None:
    """One application's closure, inside the caller's SAVEPOINT.

    `set_status` takes its own row lock and writes the application's history
    entry and audit trail. This is the ONLY lock either sweep takes on an
    application, and nothing here reads a permit after it — see the lock order in
    the module docstring.
    """
    await applications_service.set_status(db, permit.application_id, to_status=APPLICATION_CLOSED)
    await audit.log(
        db,
        action=service.PERMIT_CLOSE_APPLICATION,
        user_id=None,
        object_type=service.OBJECT_TYPE,
        object_id=permit.id,
        old_value={"application_status": service.APPLICATION_PERMIT_ISSUED},
        new_value={"application_status": APPLICATION_CLOSED},
        correlation_id=correlation,
    )


TICKET_EXPIRED_STATUS = "expired"


async def expire_forest_tickets(
    db: AsyncSession, *, limit: int = BATCH_SIZE, after_id: uuid.UUID | None = None
) -> SweepBatch:
    """Move tickets whose `valid_to` is past out of `active`.

    A STANDALONE job with its own keyset cursor (ruling 14, revised) — not a
    third statement folded into `expire_permits`'s own batch loop above. A
    ВМҚ 506 ticket's period lives INSIDE the permit's (ruling 12) but need not
    END when the permit's does, so a ticket can lapse while the permit under
    it is still `active`; coupling the two sweeps would tie together two
    lifecycles that only partially overlap. It needs no new advisory-lock
    story either: the scheduler's `ADVISORY_LOCK` (decision #36) decides
    WHICH CLUSTER INSTANCE runs, not which job does, so this registers beside
    `expire_permits` and `close_finished_permits` under that same lock.

    `valid_to < business_today()` — INCLUSIVE, like the permit's own period,
    and Asia/Tashkent, never the server's date (lesson). No notification: a
    ticket lapsing on its own last day is the calendar, not an event — the
    same silence `expire_permits` does NOT keep for its own permit, because a
    permit's holder has money and a document riding on it and a ticket's
    holder has neither.

    Same per-row SAVEPOINT as the permit sweep, so one bad row costs itself
    and not its batch.
    """
    today = business_today()
    rows = await repo.tickets_ending_before(db, today, limit=limit, after_id=after_id)
    correlation = f"job:{uuid.uuid4()}"
    # Read every id BEFORE any SAVEPOINT opens — see `expire_permits` for why
    # an id read after a rollback is a `MissingGreenlet`, not a value.
    ids = [ticket.id for ticket in rows]
    processed = failed = 0
    for ticket, ticket_id in zip(rows, ids, strict=True):
        try:
            async with db.begin_nested():
                await _expire_one_ticket(db, ticket, correlation=correlation)
        except Exception as exc:
            failed += 1
            logger.error(
                "job.expire_forest_tickets.row_failed",
                ticket_id=str(ticket_id),
                error=repr(exc),
                exc_info=True,
            )
        else:
            processed += 1
    return SweepBatch(
        scanned=len(rows), processed=processed, failed=failed, last_id=ids[-1] if ids else None
    )


async def _expire_one_ticket(db: AsyncSession, ticket: ForestTicket, *, correlation: str) -> None:
    """One ticket's whole expiry, inside the caller's SAVEPOINT."""
    ticket.status = TICKET_EXPIRED_STATUS
    await db.flush()
    await audit.log(
        db,
        action=service.FOREST_TICKET_EXPIRE,
        user_id=None,
        object_type="forest_ticket",
        object_id=ticket.id,
        old_value={"status": "active"},
        new_value={"status": TICKET_EXPIRED_STATUS},
        correlation_id=correlation,
    )
