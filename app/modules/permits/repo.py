"""Every SQL statement this module issues. No business rules live here — the
service decides, the repo asks.

The one statement worth reading twice is `next_number`: it is the whole of
ruling 9's race-freedom, and its failure mode is silence."""

import uuid
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Row, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter
from app.db import Base
from app.modules.admin.models import ActivityType, Organization
from app.modules.permits.models import (
    ForestTicket,
    Permit,
    PermitDuplicate,
    PermitRating,
    PermitStatusHistory,
    PermitTemplate,
)


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
    """`permit_by_id`'s locking sibling — `service.add_signature` and
    `service.set_status`, and the same shape as
    `applications.repo.get_application_for_update` (review C1).

    Both callers are read-check-write over one permit, which is the whole test
    for using this instead of `permit_by_id`: `add_signature` decides activation
    from a status it then writes, and `set_status` validates a `tz/05` edge out
    of a status it then writes. A plain READ stays lock-free — `permit_card`,
    `list_permits` and the public check all use `permit_by_id`.

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

    `set_status`'s own race is the plainer one: 3.11b's revoke and 4.7's archival
    can arrive together, and without the lock both would validate against the
    same pre-write status and the second UPDATE would silently overwrite the
    first, leaving a timeline claiming two transitions out of a status the permit
    was only in once.

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


async def add_duplicate(db: AsyncSession, row: PermitDuplicate) -> None:
    """One нусха register row (Task 5, ruling 9). Nothing supersedes or
    updates a row here — a register only ever grows."""
    db.add(row)
    await db.flush()


async def duplicates(db: AsyncSession, permit_id: uuid.UUID) -> Sequence[PermitDuplicate]:
    """A permit's whole register, newest first — `(issued_at, id)` both
    descending, the mirror of `status_history`'s ascending pair and for the
    same reason: `issued_at` is `server_default=func.now()`, which in
    PostgreSQL is transaction start time, so two duplicates issued in the
    same transaction share it to the microsecond and `id` (uuid7, therefore
    time-ordered) is what keeps the newest-first order stable.
    """
    rows = await db.execute(
        select(PermitDuplicate)
        .where(PermitDuplicate.permit_id == permit_id)
        .order_by(PermitDuplicate.issued_at.desc(), PermitDuplicate.id.desc())
    )
    return rows.scalars().all()


# --- Task 4: the citizen's rating ---------------------------------------------


async def rating_for_permit(db: AsyncSession, permit_id: uuid.UUID) -> PermitRating | None:
    """The citizen's rating of this permit, if one exists. `permit_ratings.permit_id`
    is UNIQUE (migration 0039), so this is `scalar_one_or_none` and never a list.

    Two callers, two reasons: `service.rate_permit` checks this explicitly so a
    second attempt is refused with a real error rather than left to this same
    index raising an uncaught `IntegrityError` (409 the service's way, not a
    500); `service.permit_card` folds the answer straight into the card so the
    cabinet needs one request for a permit and its rating, not two.
    """
    return (
        await db.execute(select(PermitRating).where(PermitRating.permit_id == permit_id))
    ).scalar_one_or_none()


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
    db: AsyncSession,
    day: date,
    *,
    statuses: Sequence[str],
    limit: int,
    after_id: uuid.UUID | None = None,
) -> list[Permit]:
    """One BATCH of permits in `statuses` whose period has run out before `day`,
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

    `suspended` joins `active` in the candidate set (Task 7, ruling 8):
    `PERMIT_TRANSITIONS` has always allowed `suspended -> expired`, and without
    it a permit suspended in June with a period ending in September would stay
    `suspended` forever — its application never reaching `close_finished`
    either.
    """
    conditions: list[ColumnElement[bool]] = [
        Permit.status.in_(statuses),
        Permit.period_to < day,
    ]
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


async def stalled_permits(
    db: AsyncSession, day: date, *, limit: int, after_id: uuid.UUID | None = None
) -> list[Permit]:
    """One BATCH of `pending_signatures` permits whose period ended before `day`
    (Task 7, ruling 16), in id order after `after_id` — `permits_ending_before`'s
    idiom for the keyset cursor, without that function's reason for `FOR UPDATE`.

    No lock: unlike the expiry sweep, `jobs.watch_stalled_permits` never moves
    the permit it reads — it only notifies, and the once-only guard is
    `notifications.already_notified`, not a status change that would need one
    row unavailable to concurrent readers while it commits. `period_to < day`,
    never `<=`, the same INCLUSIVE reading `permits_ending_before` documents;
    `day` is the caller's `business_today()`.
    """
    conditions: list[ColumnElement[bool]] = [
        Permit.status == "pending_signatures",
        Permit.period_to < day,
    ]
    if after_id is not None:
        conditions.append(Permit.id > after_id)
    rows = await db.execute(select(Permit).where(*conditions).order_by(Permit.id).limit(limit))
    return list(rows.scalars().all())


# --- Task 6: the forest ticket (ЧТ), ВМҚ 506 ----------------------------------


async def forest_tickets(db: AsyncSession, permit_id: uuid.UUID) -> Sequence[ForestTicket]:
    """A permit's whole ВМҚ 506 register, newest first — the same
    `(created_at, id)` descending tie-break `duplicates` uses above and for
    the same reason: `created_at` is `server_default=func.now()` (PostgreSQL
    transaction start time), so two tickets issued in one transaction share
    it to the microsecond and `id` (uuid7, therefore time-ordered) is what
    keeps them in a stable order.
    """
    rows = await db.execute(
        select(ForestTicket)
        .where(ForestTicket.permit_id == permit_id)
        .order_by(ForestTicket.created_at.desc(), ForestTicket.id.desc())
    )
    return rows.scalars().all()


async def live_forest_tickets(db: AsyncSession, permit_id: uuid.UUID) -> Sequence[ForestTicket]:
    """The permit's currently `active` tickets, for
    `service.revoke_tickets_of` (ruling 14) to move to `revoked` in the SAME
    transaction as the permit's own revocation. `uq_forest_tickets_active`'s
    own guarantee is that there is at most one.

    `FOR UPDATE` — the same read-check-write shape `permit_by_id_for_update`
    and `permits_ending_before` already use, and for the same reason: a
    plain SELECT never waits on another transaction's row lock, so a ticket
    `jobs.expire_forest_tickets` is expiring RIGHT NOW under its OWN lock
    would still read here as `active`, and writing `revoked` once that
    sweep's transaction commits would silently overwrite `expired` back to
    `revoked`. Blocking here until the sweep resolves, then re-reading under
    the lock (`populate_existing`), is what makes this exactly as safe as
    the bulk `UPDATE ... WHERE status = 'active'` it replaces — that
    statement's own WHERE clause is re-evaluated at write time, which is the
    property this FOR UPDATE reproduces by a different mechanism.
    """
    rows = await db.execute(
        select(ForestTicket)
        .where(ForestTicket.permit_id == permit_id, ForestTicket.status == "active")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return rows.scalars().all()


async def tickets_ending_before(
    db: AsyncSession, day: date, *, limit: int, after_id: uuid.UUID | None = None
) -> list[ForestTicket]:
    """One BATCH of `active` tickets whose `valid_to` has run out before
    `day`, locked, in id order after `after_id` — `jobs.expire_forest_tickets`'s
    own keyset cursor (Task 6, ruling 14 revised: a STANDALONE sweep, not a
    third statement folded into `expire_permits`'s own batch loop), the same
    shape `permits_ending_before` above already uses for the permit sweep and
    for the same reasons:

    `valid_to < day`, never `<=`: `forest_tickets.valid_to` is INCLUSIVE, so a
    ticket ending today is still in force today. `day` is the caller's
    `business_today()` — Asia/Tashkent, never the server's own date.

    `FOR UPDATE` because this is a read-check-write over rows another actor
    (a revocation) can move at the same time; `populate_existing` is the ORM
    half of that same guarantee — without it the loader keeps whatever an
    instance already in the identity map was holding (lesson).

    **`limit` + `after_id` is a keyset cursor, not a page number** — the same
    reasoning `permits_ending_before` gives: those `FOR UPDATE` locks are
    held until the caller commits, so the batch size bounds how long a swept
    ticket is unavailable to anything else, and the cursor is what lets the
    worker resume after a batch it could not finish.
    """
    conditions: list[ColumnElement[bool]] = [
        ForestTicket.status == "active",
        ForestTicket.valid_to < day,
    ]
    if after_id is not None:
        conditions.append(ForestTicket.id > after_id)
    rows = await db.execute(
        select(ForestTicket)
        .where(*conditions)
        .order_by(ForestTicket.id)
        .limit(limit)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def active_stats_by_organization(db: AsyncSession) -> list[tuple[uuid.UUID, int, Decimal]]:
    """`(organization_id, active_count, active_area_ha)` for every organization
    holding at least one `active` permit (4.6 `public`'s open-data aggregate —
    the anonymous surface never sees a single permit row, only this sum).
    `area_ha` is the permit's own frozen figure (design/02 § permits), not a
    live GIS read — an open-data number should not move under the same permit
    because a contour was re-surveyed.
    """
    rows = await db.execute(
        select(
            Permit.organization_id,
            func.count(Permit.id),
            func.coalesce(func.sum(Permit.area_ha), 0),
        )
        .where(Permit.status == "active")
        .group_by(Permit.organization_id)
    )
    return [(row[0], row[1], row[2]) for row in rows.all()]


# --- Task 5: the Agency's aggregates, without the author (ruling #142) --------


def _day_bounds(period_from: date, period_to: date) -> tuple[datetime, datetime]:
    """A calendar-day `[from, to]` window as an inclusive timestamptz range —
    `permit_ratings.created_at` is `timestamptz`, so the boundary must be a
    moment, not a bare date (the same reasoning, and the same shape,
    `dashboard.repo._day_bounds` gives its own callers — duplicated rather
    than imported, since `dashboard` is a level-5 READER of `permits` and may
    not be imported the other way)."""
    return datetime.combine(period_from, time.min), datetime.combine(period_to, time.max)


def _ratings_conditions(
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> list[Any]:
    """The clauses every query below shares: the actor's own zone, on ALL
    THREE axes (region, district AND organization — `dashboard.repo.
    permits_kpi`'s own reasoning: `organization_id` alone passes a
    region-or-district-scoped actor for every organization in the country),
    the period, and the two optional narrowing filters the route accepts.
    `organization_id`/`activity_type_id` default to `None` so a caller passing
    neither gets the unnarrowed set; both routes — the summary and the comment
    feed — pass them through, which is what keeps a narrowed summary and the
    comments below it describing the same population."""
    conditions: list[Any] = [
        zone_filter(
            actor_zone,
            region_col=Organization.region_id,
            district_col=Organization.district_id,
            organization_col=Organization.id,
        ),
        PermitRating.created_at.between(*_day_bounds(period_from, period_to)),
    ]
    if organization_id is not None:
        conditions.append(Permit.organization_id == organization_id)
    if activity_type_id is not None:
        conditions.append(Permit.activity_type_id == activity_type_id)
    return conditions


async def ratings_overall(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> tuple[Decimal | None, int]:
    """`(avg_score, count)` over every rating the actor's zone and the given
    filters admit. `ROUND(AVG(score), 2)` runs IN SQL, never in Python
    (`schemas.RatingsSummaryOut`'s own docstring: the resulting `Decimal`
    already has scale 2, which is what makes `"4.00"` come out the wire
    rather than `"4"`). `AVG` over zero rows is SQL `NULL`, and `ROUND(NULL,
    2)` stays `NULL` — asyncpg hands that back as `None`, so a zone with no
    ratings in the period reads as `(None, 0)`, never a division by zero.
    """
    conditions = _ratings_conditions(
        actor_zone=actor_zone,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    stmt = (
        select(func.round(func.avg(PermitRating.score), 2), func.count())
        .select_from(PermitRating)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    avg_score, count = (await db.execute(stmt)).one()
    return avg_score, count


async def ratings_by_organization(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> Sequence[Row[Any]]:
    """`(organization_id, name, avg_score, count)`, one row per organization
    with at least one rating in scope — the summary card's left-hand
    breakdown."""
    conditions = _ratings_conditions(
        actor_zone=actor_zone,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    stmt = (
        select(
            Permit.organization_id,
            Organization.name,
            func.round(func.avg(PermitRating.score), 2),
            func.count(),
        )
        .select_from(PermitRating)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
        .group_by(Permit.organization_id, Organization.name)
    )
    return (await db.execute(stmt)).all()


async def ratings_by_activity_type(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date,
    period_to: date,
) -> Sequence[Row[Any]]:
    """`(activity_type_id, name, avg_score, count)`, one row per activity type
    with at least one rating in scope. Still joins `organizations`, even
    though the grouping is by activity type: the zone clause needs its
    region/district columns."""
    conditions = _ratings_conditions(
        actor_zone=actor_zone,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    stmt = (
        select(
            Permit.activity_type_id,
            ActivityType.name,
            func.round(func.avg(PermitRating.score), 2),
            func.count(),
        )
        .select_from(PermitRating)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .join(ActivityType, ActivityType.id == Permit.activity_type_id)
        .where(*conditions)
        .group_by(Permit.activity_type_id, ActivityType.name)
    )
    return (await db.execute(stmt)).all()


async def rating_comments(
    db: AsyncSession,
    *,
    actor_zone: Zone,
    organization_id: uuid.UUID | None = None,
    activity_type_id: uuid.UUID | None = None,
    period_from: date,
    period_to: date,
    offset: int,
    limit: int,
) -> tuple[Sequence[Row[Any]], int]:
    """One page of the anonymous comment feed, newest first, with the total —
    the same count-then-select shape `list_permits` uses. `organization_id`/
    `activity_type_id` narrow the same way they narrow `/admin/ratings/
    summary` — a screen that filters the summary to one leshoz must be able to
    filter the comment feed under it the same way, or the two describe
    different populations with nothing saying so (final review, finding 3).
    Both default to `None` so a caller that wants only the zone/period scope
    (none did before this fix) still gets it.

    Every column named here is `RatingCommentRow`'s whole contract —
    `created_at`, `score`, `comment`, the organization's and the activity
    type's own `name`, each aliased to the schema's own field name so the
    service can build the row straight off `Row._mapping` — and nothing else:
    never `PermitRating.permit_id`, never anything from `permits.applicant_id`
    (ruling #141).
    """
    conditions = _ratings_conditions(
        actor_zone=actor_zone,
        organization_id=organization_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
    )
    joined = (
        select(PermitRating.id)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    rows = await db.execute(
        select(
            PermitRating.created_at,
            PermitRating.score,
            PermitRating.comment,
            Organization.name.label("organization_name"),
            ActivityType.name.label("activity_type_name"),
        )
        .select_from(PermitRating)
        .join(Permit, Permit.id == PermitRating.permit_id)
        .join(Organization, Organization.id == Permit.organization_id)
        .join(ActivityType, ActivityType.id == Permit.activity_type_id)
        .where(*conditions)
        .order_by(PermitRating.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return rows.all(), total
