"""Every SQL statement this module issues. No business rules live here — the
service decides, the repo asks.

The one statement worth reading twice is `next_number`: it is the whole of
ruling 9's race-freedom, and its failure mode is silence."""

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.admin.models import Organization
from app.modules.permits.models import Permit, PermitStatusHistory, PermitTemplate


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def next_number(db: AsyncSession, series: str) -> int | None:
    """The next number in `series`, or None when the series has no counter row.

    One `UPDATE ... RETURNING` inside the caller's transaction (design/02 §
    permit_counters, ruling 9), never SELECT-then-UPDATE: the row lock is held
    until that transaction ends, so two concurrent issuances queue instead of
    reading the same value and handing out one number twice.

    None is the misconfiguration case and the caller must refuse on it. The
    series is CYRILLIC А (U+0410); a Latin A (U+0041) is a different key, matches
    no row, and this statement then reports success while returning nothing —
    which is how a permit would be written with no number at all.
    """
    return await db.scalar(
        text(
            "UPDATE permit_counters SET last_number = last_number + 1"
            " WHERE series = :series RETURNING last_number"
        ).bindparams(series=series)
    )


async def permit_by_id(db: AsyncSession, permit_id: uuid.UUID) -> Permit | None:
    return await db.get(Permit, permit_id)


async def permit_by_id_for_update(db: AsyncSession, permit_id: uuid.UUID) -> Permit | None:
    """`permit_by_id`'s locking sibling — `service.add_signature` ONLY, and the
    same shape as `applications.repo.get_application_for_update` (review C1).

    `SELECT ... FOR UPDATE` so two signatories landing at the same instant
    serialise instead of racing. Without it, under READ COMMITTED, the third and
    fourth signatories each insert their own `signatures` row — different
    purposes, so `uq_signatures_valid_purpose` never fires — and then each asks
    `missing_purposes`, neither seeing the other's UNCOMMITTED row. Both get a
    non-empty list, neither activates, both commit: a permit carrying four valid
    signatures, stuck in `pending_signatures`, with its application stuck in
    `PAID`. There is no recovery path — a retry passes the status check, `sign()`
    then answers `ERR-SIGN-002`, and `_activate` is reachable from nowhere else —
    so it takes a hand-edit of the database. The second caller here blocks until
    the first commits, then reads a `missing_purposes` that includes it.

    `populate_existing=True` is what makes that re-read true, and is mechanical:
    `with_for_update` alone does emit a real `SELECT ... FOR UPDATE`, but the
    loader then refreshes only the attributes an already-held instance has NOT
    loaded, and `app/db.py`'s `expire_on_commit=False` never clears the rest —
    so the lock would be taken and a stale `status` validated under it.
    `tests/test_code_conventions.py::test_every_locking_get_also_repopulates_the_row`
    enforces the pairing.
    """
    return await db.get(Permit, permit_id, with_for_update=True, populate_existing=True)


async def permit_by_application(db: AsyncSession, application_id: uuid.UUID) -> Permit | None:
    """The permit issued for this application, or None. `permits.application_id`
    is unique (design/02), so this is a 1:1 lookup and never a list."""
    return (
        await db.execute(select(Permit).where(Permit.application_id == application_id))
    ).scalar_one_or_none()


async def permit_by_qr_token(db: AsyncSession, qr_token: str) -> Permit | None:
    """The permit a printed QR points at, or None (Task 5's public check).

    `permits.qr_token` is unique, so this is `scalar_one_or_none` and never a
    list. The token is `secrets.token_urlsafe(32)` and is compared here as an
    indexed equality rather than with `secrets.compare_digest`: the latter
    raises `TypeError` on a non-ASCII operand, and this argument comes straight
    off a query string a stranger writes (lesson).
    """
    return (
        await db.execute(select(Permit).where(Permit.qr_token == qr_token))
    ).scalar_one_or_none()


async def permit_by_series_number(db: AsyncSession, series: str, number: int) -> Permit | None:
    """The permit a citizen holding a paper copy can name — «серия А № 000123».

    `uq_permits_series_number` makes the pair unique, so this too is a 1:1
    lookup. `number` must already be bounded to `bigint` by the caller: an
    out-of-range integer reaches asyncpg as a bind parameter and raises, which
    on an anonymous route is a 500 handed out for free.
    """
    return (
        await db.execute(select(Permit).where(Permit.series == series, Permit.number == number))
    ).scalar_one_or_none()


async def status_history(db: AsyncSession, permit_id: uuid.UUID) -> list[PermitStatusHistory]:
    """The permit's timeline, oldest first.

    Ordered by `(occurred_at, id)` and never by `occurred_at` alone, exactly as
    `models.PermitStatusHistory` requires: `occurred_at` defaults to `now()`,
    which in PostgreSQL is TRANSACTION start time, so the issuance row and any
    row written in that same transaction share it to the microsecond and the
    tie-break is the only thing that keeps the timeline in order. `id` is uuid7
    and therefore time-ordered, and `ix_permit_status_history_timeline` carries
    all three columns so this ordering is served by the index.
    """
    rows = await db.execute(
        select(PermitStatusHistory)
        .where(PermitStatusHistory.permit_id == permit_id)
        .order_by(PermitStatusHistory.occurred_at, PermitStatusHistory.id)
    )
    return list(rows.scalars().all())


async def list_permits(
    db: AsyncSession,
    *,
    scope: Any,
    status: str | None,
    applicant_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    organization_id: uuid.UUID | None,
    series: str | None,
    number: int | None,
    offset: int,
    limit: int,
) -> tuple[list[Permit], int]:
    """One page of permits matching `scope` and the given filters, with the total.

    `scope` is whatever the service built out of the caller's identity — an
    `applicant_id IN (...)` for a holder, `abac.zone_filter`'s expression for
    staff, or the OR of both. It is a required positional-by-keyword argument
    with no default on purpose: a read of this table with no scope at all is
    every permit in the country, and a default would make that the easy mistake.

    Joins `organizations` (the shape `gis.repo.list_contours` already uses for
    the same reason): `permits` carries `organization_id` but no region or
    district, and `zone_filter` FAILS CLOSED — it raises when a zone axis is set
    and its column was not supplied — so a region- or district-scoped actor,
    which `admin.users_service.create_user` can create today, needs those two
    columns present in the statement.

    Newest first by `id`: it is uuid7 and therefore time-ordered, so this is
    `created_at DESC` served by the primary key rather than by a second index
    (the same tie-break reasoning `norms.repo.newest_calculation` uses).
    """
    conditions: list[Any] = [scope]
    for column, value in (
        (Permit.status, status),
        (Permit.applicant_id, applicant_id),
        (Permit.contour_id, contour_id),
        (Permit.organization_id, organization_id),
        (Permit.series, series),
        (Permit.number, number),
    ):
        if value is not None:
            conditions.append(column == value)
    joined = (
        select(Permit.id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    rows = await db.execute(
        select(Permit)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
        .order_by(Permit.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total


async def active_template(db: AsyncSession, activity_type_id: uuid.UUID) -> PermitTemplate | None:
    """The one layout in force for an activity type. `uq_permit_templates_active`
    (a partial unique index over `status = 'active'`) is what makes "the one"
    true, so this can be `scalar_one_or_none` rather than an ordered `first()`
    that would quietly pick a winner."""
    return (
        await db.execute(
            select(PermitTemplate).where(
                PermitTemplate.activity_type_id == activity_type_id,
                PermitTemplate.status == "active",
            )
        )
    ).scalar_one_or_none()


async def add_status_history(db: AsyncSession, row: PermitStatusHistory) -> None:
    """`permit_status_history` is append-only at the database level (migration
    0019's trigger). A correction is a new row, never an UPDATE."""
    db.add(row)
    await db.flush()


async def occupied_area_by_contour(
    db: AsyncSession, contour_ids: Sequence[uuid.UUID], *, status: str
) -> dict[uuid.UUID, Decimal]:
    """How many hectares each of these contours has committed, in ONE statement.

    Batch-shaped because 3.6a reshaped `gis.service.OCCUPANCY_PROVIDERS` to be
    batch-shaped: `list_contours` needs one answer per row, and a per-contour
    query would make a page of 20 twenty round-trips (the whole-country list
    ~13,500 — the seam's own comment). `GROUP BY` is what keeps that promise.

    Contours with nothing on them are simply absent from the result; the seam's
    contract says a key it was not given back counts as zero, so there is no
    reason to pay for a LEFT JOIN against a list of ids.

    `status` comes from the caller: `service.ACTIVE_STATUS` is the module's one
    source of truth for that word and importing the service from here would be a
    cycle.
    """
    rows = await db.execute(
        select(Permit.contour_id, func.sum(Permit.area_ha))
        .where(Permit.status == status, Permit.contour_id.in_(contour_ids))
        .group_by(Permit.contour_id)
    )
    return {contour_id: total for contour_id, total in rows.all()}


async def committed_sb_load(
    db: AsyncSession,
    contour_id: uuid.UUID,
    period_from: date,
    period_to: date,
    *,
    status: str,
) -> Decimal:
    """The conditional heads already committed on this contour over any part of
    `[period_from, period_to]`.

    The overlap predicate is `permit.period_from <= :period_to AND
    permit.period_to >= :period_from` — both ends inclusive, because both ends of
    a permit's own period are days of use (`period_to` is the last day, not the
    day after). Two herds sharing a single day share the pasture that day.

    A reversed argument pair inverts this predicate and hides the rows it should
    find (lesson) — `norms.checks.run_checks`, the shared entry point every
    caller reaches this through, refuses one fail-closed before any of this runs.

    `sb_load` is null for an activity that commits no conditional-head load at
    all, and `SUM` skips nulls; `COALESCE` turns the all-null (and the no-row)
    answer into a real `Decimal("0")` rather than a `None` the caller would have
    to add to a total.
    """
    total = await db.scalar(
        select(func.coalesce(func.sum(Permit.sb_load), 0)).where(
            Permit.status == status,
            Permit.contour_id == contour_id,
            Permit.period_from <= period_to,
            Permit.period_to >= period_from,
        )
    )
    return Decimal(total or 0)


async def permits_ending_before(
    db: AsyncSession, day: date, *, status: str, limit: int, after_id: uuid.UUID | None = None
) -> list[Permit]:
    """One BATCH of permits in `status` whose period has run out before `day`,
    locked, in id order after `after_id`.

    `period_to < day` and never `<=`: `permits.period_to` is INCLUSIVE, so a
    permit ending today is still in force today (`tz/05`). `day` is the caller's
    `business_today()` — Asia/Tashkent, never the server's own date.

    `FOR UPDATE` because this is a read-check-write over rows another actor can
    move at the same time (3.11b's suspend/revoke). Under READ COMMITTED,
    Postgres re-evaluates the WHERE clause against the row version it finally
    locks, so a permit revoked while the sweep waited simply drops out of the
    result instead of being expired on top of the revocation. `populate_existing`
    is the ORM half of the same guarantee: without it the loader keeps whatever
    an instance already in the identity map was holding (lesson), and the sweep
    would decide on a stale status under a correct lock.

    **`limit` + `after_id` is a keyset cursor, not a page number.** Those
    `FOR UPDATE` locks are held until the caller commits, so the batch size is
    what bounds how long a swept permit is unavailable to anything else (review,
    Important 3) — and the cursor, not `OFFSET`, is what lets the worker resume
    after a batch it could not finish. A row that has been expired leaves the
    result set anyway, so ids only ever move forward and nothing is visited twice.

    Permits, then applications: `service.add_signature` locks in that order and
    it is the only lock ordering anywhere in `app/` — a sweep that took an
    application lock first would close the cycle.
    """
    conditions: list[ColumnElement[bool]] = [Permit.status == status, Permit.period_to < day]
    if after_id is not None:
        conditions.append(Permit.id > after_id)
    rows = await db.execute(
        select(Permit)
        .where(*conditions)
        .order_by(Permit.id)
        .limit(limit)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def permits_in_statuses(
    db: AsyncSession, statuses: Sequence[str], *, limit: int, after_id: uuid.UUID | None = None
) -> list[Permit]:
    """One BATCH of the permits that have reached one of `statuses` — the closure
    sweep's candidate set (`expired`/`revoked`) — in id order after `after_id`.

    Unlocked: the sweep does not write the permit at all, it moves the
    APPLICATION, and the lock that matters is the one
    `applications.service.set_status` takes on the row it does write.

    `limit` bounds the TRANSACTION, not the day: unlike the expiry sweep this
    candidate set does NOT shrink as the work is done (a closed application
    leaves its permit exactly where it was), so the worker keeps asking with the
    cursor advanced until a short batch says the set is drained. A LIMIT with no
    cursor would have been the thing worth refusing — it would cap how many
    permits may finish in one day, and grazing seasons end on the same date for
    whole districts at a time.
    """
    conditions: list[ColumnElement[bool]] = [Permit.status.in_(statuses)]
    if after_id is not None:
        conditions.append(Permit.id > after_id)
    rows = await db.execute(select(Permit).where(*conditions).order_by(Permit.id).limit(limit))
    return list(rows.scalars().all())
