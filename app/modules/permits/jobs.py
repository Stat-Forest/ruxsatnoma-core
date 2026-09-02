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
from app.modules.permits.models import Permit, PermitStatusHistory

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

    Only `active` is swept. `pending_signatures` never came into force (C11), so
    «муддати тугаган» would be a false statement about it on the public check
    page; `suspended` and `revoked` are 3.11b's, which owns what a period ending
    means for a permit already out of use. `permits_ending_before` takes the
    candidates `FOR UPDATE`, so a permit revoked while this sweep waited drops
    out of the result instead of being expired on top of the revocation.

    One row's status change, its history row, the holder's notification and its
    audit entry share a SAVEPOINT: all four land or none of them do, so a permit
    can never be left expired with nobody told, and a row that raises rolls back
    alone instead of taking its batch with it.
    """
    today = business_today()
    rows = await repo.permits_ending_before(
        db, today, status=service.ACTIVE_STATUS, limit=limit, after_id=after_id
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
    permit.status = EXPIRED_STATUS
    # Flushes the status change with it, which is what makes a second run
    # inside this same transaction find nothing (the idempotency test runs
    # the sweep twice without committing in between).
    await repo.add_status_history(
        db,
        PermitStatusHistory(
            permit_id=permit.id,
            from_status=service.ACTIVE_STATUS,
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
        params={"permit_number": service._permit_number(permit.series, permit.number)},
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
        old_value={"status": service.ACTIVE_STATUS},
        new_value={"status": EXPIRED_STATUS},
        correlation_id=correlation,
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
