"""Queries and writes for `applications`. Nothing here decides anything —
transition legality, permission and zone rules all belong to `service.py`;
repo only reads and writes rows (design/01 rule 2: router -> service -> repo
-> models). Branch 1 (`stage-3.9a-core`) needed exactly the first three
functions below, for `service.get` and `service.set_status`; branch 2's task 3
adds the draft's own reads and writes, and later tasks add the rest (the
duplicate-guard read, submission writes, precheck/decision queries).

`Organization` is read here for ONE reason and in ONE place: `list_applications`
JOINs it so a region- or district-scoped actor's zone can be enforced.
`applications` carries an organization id and no region or district, and
`abac.zone_filter` FAILS CLOSED — it raises when a zone axis is set and its
column was not supplied. Reference data is read-only to every module
(CLAUDE.md); the CONTOUR half of that same JOIN is not ours to build and is not
ours to FETCH either, so `service.list_applications` obtains it from
`gis.service.contour_organization_column` and passes it in as an expression —
this file imports no other module's service (review I2)."""

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.applications.assignment import Candidate
from app.modules.applications.models import (
    Application,
    ApplicationAssignment,
    ApplicationCheck,
    ApplicationConclusion,
    ApplicationDocument,
    ApplicationItem,
    ApplicationStatusHistory,
    InfoRequest,
)
from app.modules.applications.sla import SLA_ACTIVE_STATUSES


async def get_application(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await db.get(Application, application_id)


async def get_application_for_update(
    db: AsyncSession, application_id: uuid.UUID
) -> Application | None:
    """`service.get`'s locking sibling — for a WRITE path only, never a read
    (review C1). Branch 1 said "`set_status` ONLY", true while `set_status` was
    the only write path in the module; branch 2's `patch_draft` takes the same
    lock through `service._own_draft_for_update`, for the same reason and with
    the same read-check-write shape over `status`.
    `SELECT ... FOR UPDATE` so two concurrent transitions on the same
    application serialise instead of racing: without it, two callers who
    both read the same pre-write status (a scheduler job and an HTTP
    callback, say) can both pass `_assert_transition` and both write, and
    the loser's UPDATE silently overwrites the winner's — exactly the bug
    `notifications.service._deliver`'s own `with_for_update` already exists
    to prevent for the identical read-check-write shape. The second caller
    here blocks until the first commits or rolls back, then re-reads the
    now-current status, so a genuine conflict surfaces as `ERR-APP-004`
    instead of a lost write.

    `populate_existing=True` is what makes "re-reads" true (final review C2).
    `with_for_update` alone does emit a real `SELECT ... FOR UPDATE` — it
    skips `Session.get`'s identity-map shortcut — but the loader then takes
    its PARTIAL-population branch for an instance the session already holds
    and refreshes only the attributes that are unloaded, so a caller who ran
    `service.get(...)` first keeps its cached `status`; `app/db.py`'s
    `expire_on_commit=False` means a commit in between does not clear it
    either. The lock would be taken and the stale value validated: exactly
    the lost update above, with the lock in place. `app/core/idempotency.py`
    documents the identical trap on `IdempotencyKey` and fixes it the same
    way. Autoflush runs before the SELECT, so pending work on this row is
    written and read back rather than discarded."""
    return await db.get(Application, application_id, with_for_update=True, populate_existing=True)


async def add_status_history(db: AsyncSession, entry: ApplicationStatusHistory) -> None:
    """Stage the entry and flush — together with whatever else is dirty on
    the session (`set_status`'s own `applications.status` UPDATE), so the
    append-only trigger and the two status CHECK constraints surface at the
    call site. Mirrors `audit.repo.add`."""
    db.add(entry)
    await db.flush()


async def list_items(db: AsyncSession, application_id: uuid.UUID) -> list[ApplicationItem]:
    """The application's livestock lines, in a stable order (`uuid7` is
    time-ordered, so this is insertion order served by the primary key)."""
    rows = await db.execute(
        select(ApplicationItem)
        .where(ApplicationItem.application_id == application_id)
        .order_by(ApplicationItem.id)
    )
    return list(rows.scalars().all())


async def replace_items(
    db: AsyncSession, application_id: uuid.UUID, items: list[ApplicationItem]
) -> None:
    """Wholesale replacement, never a merge: an applicant removing a livestock
    kind must be able to (plan task 3). The DELETE and the INSERTs are one
    statement pair inside the caller's transaction, and the `flush()` between
    them is what lets a PATCH re-send the SAME `livestock_type_id` — without it
    the old row and the new one are both pending when
    `uq_application_items_livestock` is checked and the insert raises
    `IntegrityError` on a conflict the flush would have resolved (lesson: a
    partial/unique index is checked at flush, so a supersede write flushes
    between the removal and the insert)."""
    await db.execute(
        delete(ApplicationItem).where(ApplicationItem.application_id == application_id)
    )
    await db.flush()
    for item in items:
        db.add(item)
    await db.flush()


async def list_documents(db: AsyncSession, application_id: uuid.UUID) -> list[ApplicationDocument]:
    """The attachments, oldest first (`uuid7` is time-ordered, so the primary
    key already serves this)."""
    rows = await db.execute(
        select(ApplicationDocument)
        .where(ApplicationDocument.application_id == application_id)
        .order_by(ApplicationDocument.id)
    )
    return list(rows.scalars().all())


async def get_document(db: AsyncSession, document_id: uuid.UUID) -> ApplicationDocument | None:
    return await db.get(ApplicationDocument, document_id)


async def add_document(db: AsyncSession, document: ApplicationDocument) -> None:
    """Stage and flush, then read the row back: `created_at` is a
    `server_default` and the 201 response serializes it (lesson: the row in
    memory is not what Postgres stored)."""
    db.add(document)
    await db.flush()
    await db.refresh(document)


async def delete_document(db: AsyncSession, document: ApplicationDocument) -> None:
    """A real DELETE, and legitimately so: `application_documents` carries no
    append-only trigger (migration 0015 puts one on
    `application_status_history` alone), and an applicant unpicking an
    attachment from their own draft is not a fact the register needs to keep —
    the audit entry the service writes beside this is. The `media_files` row
    itself is untouched: files are never deleted (`status='archived'`).
    """
    await db.delete(document)
    await db.flush()


async def list_conclusions(
    db: AsyncSession, application_id: uuid.UUID
) -> list[ApplicationConclusion]:
    """EVERY conclusion row, oldest first (`uuid7` is time-ordered) — never
    "the latest per kind": a repeat conclusion after rework is a NEW row and
    the head reads the history (ruling 10), the same reason `list_checks`
    beside it never collapses to one row per `check_type`."""
    rows = await db.execute(
        select(ApplicationConclusion)
        .where(ApplicationConclusion.application_id == application_id)
        .order_by(ApplicationConclusion.id)
    )
    return list(rows.scalars().all())


async def add_conclusion(db: AsyncSession, conclusion: ApplicationConclusion) -> None:
    """Stage and flush, then read the row back — `add_document`'s own shape:
    `created_at` is a `server_default` and the 201 response serializes it
    (lesson: the row in memory is not what Postgres stored)."""
    db.add(conclusion)
    await db.flush()
    await db.refresh(conclusion)


async def add_checks(db: AsyncSession, rows: list[ApplicationCheck]) -> None:
    """Insert one run's check rows and load their server defaults back.

    The re-SELECT is not politeness: `checked_at` is `server_default=func.now()`
    and is left unloaded by the INSERT, so the first attribute access would
    emit a lazy refresh — which on an async session raises `MissingGreenlet`
    the moment it happens during response serialization instead of inside an
    `await`. One statement for the whole run, not one `refresh` per row.
    """
    if not rows:
        return
    db.add_all(rows)
    await db.flush()
    await db.execute(
        select(ApplicationCheck).where(ApplicationCheck.id.in_([row.id for row in rows]))
    )


async def list_checks(db: AsyncSession, application_id: uuid.UUID) -> list[ApplicationCheck]:
    """EVERY check row, oldest first — never "the latest per type". A repeat
    check is a new row and the history is the evidence (ruling 12), so a
    `DISTINCT ON (check_type)` read here would quietly throw away what a
    reviewer's screen exists to show."""
    rows = await db.execute(
        select(ApplicationCheck)
        .where(ApplicationCheck.application_id == application_id)
        .order_by(ApplicationCheck.id)
    )
    return list(rows.scalars().all())


async def get_check(db: AsyncSession, check_id: uuid.UUID) -> ApplicationCheck | None:
    """`get_application`'s own shape (line ~42), for `confirm_check`'s single
    row instead of `list_checks`' whole run."""
    return await db.get(ApplicationCheck, check_id)


def _zone_join_target(contour_organization_col: Any) -> Any:
    """The organization an application's zone rule compares against:
    `assigned_org_id` while it has one, the contour's owner before a reviewer
    takes it into work.

    `assigned_org_id` is null for every DRAFT and stays null through SUBMITTED
    until `start-review` writes the assignment (plan ruling 14), so a zone rule
    reading that column alone would show a hodim an EMPTY work queue — the
    applications they are supposed to pick up are precisely the unassigned ones.

    The contour half arrives as an ARGUMENT, built by
    `gis.service.contour_organization_column` in `service.list_applications`.
    `contours` is gis's table, design/01 rule 5 does not extend to this module,
    and a repo calling another module's service inverts the layering even
    though the boundary rule allows the call — so the call is made a layer up
    and only its expression comes down here (review I2)."""
    return func.coalesce(Application.assigned_org_id, contour_organization_col)


async def list_applications(
    db: AsyncSession,
    *,
    scope: Any,
    contour_organization_col: Any,
    status: str | None,
    activity_type_id: uuid.UUID | None,
    contour_id: uuid.UUID | None,
    applicant_id: uuid.UUID | None,
    number: str | None,
    period_from: date | None,
    period_to: date | None,
    offset: int,
    limit: int,
) -> tuple[list[Application], int]:
    """One page of applications matching `scope` and the given filters, with the
    total.

    `scope` and `contour_organization_col` are both built by the service and
    passed in: the first out of the caller's identity, the second by
    `gis.service.contour_organization_column`. `scope` is whatever the service
    built out of the caller's identity — an
    `applicant_id IN (...)` for an applicant, `abac.zone_filter`'s expression
    for staff, or the OR of both. Keyword-only with no default on purpose: a
    read of this table with no scope at all is every application in the country,
    and a default would make that the easy mistake (`permits.repo.list_permits`
    states the same rule for the same reason).

    The join to `organizations` is an OUTER join, and that is load-bearing. An
    application whose organization cannot be resolved yet — a fresh DRAFT with
    no contour — must still reach its OWN applicant through the `scope`'s
    applicant branch; an inner join would drop it. A staff member's zone
    predicate over a NULL organization row is NULL, which is not true, so such a
    row stays invisible to a ZONED actor while a republic-wide one (whose
    `zone_filter` is `true()` and reads no organization column at all) still
    sees it.

    `period_from`/`period_to` filter by OVERLAP, not by equality: "applications
    active in this window" is the question a reviewer's queue asks, and an
    exact-match filter on either end would answer a question nobody has.

    Newest first by `id`: it is uuid7 and therefore time-ordered, so this is
    `created_at DESC` served by the primary key rather than by a second index.
    """
    conditions: list[Any] = [scope]
    for column, value in (
        (Application.status, status),
        (Application.activity_type_id, activity_type_id),
        (Application.contour_id, contour_id),
        (Application.applicant_id, applicant_id),
        (Application.number, number),
    ):
        if value is not None:
            conditions.append(column == value)
    if period_from is not None:
        conditions.append(Application.period_to >= period_from)
    if period_to is not None:
        conditions.append(Application.period_from <= period_to)

    join_target = _zone_join_target(contour_organization_col)
    counted = (
        select(Application.id)
        .outerjoin(Organization, Organization.id == join_target)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(counted.subquery()))).scalar_one()
    rows = await db.execute(
        select(Application)
        .outerjoin(Organization, Organization.id == join_target)
        .where(*conditions)
        .order_by(Application.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total


# tz/05 invariant 1, and the WHERE clause of migration 0015's
# `ex_applications_no_duplicate` verbatim: the statuses in which an application
# OCCUPIES its (applicant, contour, activity, period) slot. DRAFT is outside it
# on purpose — a duplicate is caught at submission, not while the applicant is
# still typing — and so is every terminal status, since a rejected or cancelled
# filing blocks nothing. The tuple and the constraint must move together.
ACTIVE_STATUSES = (
    "SUBMITTED",
    "IN_REVIEW",
    "PENDING_INFO",
    "RETURNED",
    "APPROVED",
    "INVOICED",
    "PAID",
    "PERMIT_ISSUED",
)


async def active_overlapping(
    db: AsyncSession,
    *,
    applicant_id: uuid.UUID,
    contour_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    period_from: date | None,
    period_to: date | None,
    exclude_id: uuid.UUID,
) -> Application | None:
    """The application already occupying this slot — the EXCLUDE constraint's
    own predicate, re-run as a SELECT.

    **It exists ONLY to NAME the colliding application in `ERR-APP-002`, never
    to pre-empt the insert** (ruling 6). The database is the only detector: a
    "check then insert" is a race that two clicks a millisecond apart both win,
    which is exactly why `tz/05` invariant 1 says «на уровне БД». So this runs
    AFTER the `IntegrityError`, inside `service.submit`'s savepoint recovery,
    and its answer is a message rather than a decision.

    The overlap test is `daterange(period_from, period_to, '[]') &&` written
    out: two INCLUSIVE ranges intersect exactly when each starts no later than
    the other ends. `exclude_id` keeps the row being submitted out of its own
    answer — it is already SUBMITTED in this transaction when the constraint
    fires.
    """
    if contour_id is None or activity_type_id is None or period_from is None or period_to is None:
        # The constraint's WHERE requires all three to be non-null, so a row
        # with any of them missing cannot have collided in the first place.
        return None
    rows = await db.execute(
        select(Application)
        .where(
            Application.id != exclude_id,
            Application.applicant_id == applicant_id,
            Application.contour_id == contour_id,
            Application.activity_type_id == activity_type_id,
            Application.status.in_(ACTIVE_STATUSES),
            Application.period_from <= period_to,
            Application.period_to >= period_from,
        )
        .order_by(Application.id)
        .limit(1)
    )
    return rows.scalars().first()


# 3.11b task 8 (`permits.service.extend`): the statuses in which an EXTENSION
# still blocks a second one. Unlike `ACTIVE_STATUSES` above this INCLUDES
# DRAFT — an extension nobody has submitted yet still occupies the "already
# asked" slot, which is exactly the case the "two clicks" guard exists for —
# and excludes only the five terminal ones (a rejected, cancelled, expired,
# closed or archived extension blocks nothing, the same reasoning
# `ACTIVE_STATUSES` applies to a fresh filing).
OPEN_EXTENSION_STATUSES = ("DRAFT", *ACTIVE_STATUSES)


async def open_extension_of(
    db: AsyncSession, parent_application_id: uuid.UUID
) -> Application | None:
    """The still-open `kind='extension'` child of `parent_application_id`, if
    one exists — `permits.service.extend`'s duplicate guard, in the same
    shape as `active_overlapping` above: it exists to NAME the collision in
    `ERR-APP-002`, not to pre-empt the insert. Here the guard needs no
    pre-emption at all, because the caller locks the PARENT PERMIT
    (`permit_by_id_for_update`) before reaching this read, so two concurrent
    extend attempts on the same permit serialise on that lock rather than
    racing each other to this SELECT.
    """
    rows = await db.execute(
        select(Application)
        .where(
            Application.parent_application_id == parent_application_id,
            Application.kind == "extension",
            Application.status.in_(OPEN_EXTENSION_STATUSES),
        )
        .order_by(Application.id)
        .limit(1)
    )
    return rows.scalars().first()


# --- Task 6: the timeline's rows, and the assignment register -----------------


async def list_status_history(
    db: AsyncSession, application_id: uuid.UUID
) -> list[ApplicationStatusHistory]:
    """The application's transitions, oldest first, ordered by `(occurred_at,
    id)` — **never `occurred_at` alone** (`models.ApplicationStatusHistory`'s
    own docstring, final review M3).

    `occurred_at` defaults to `now()`, which in Postgres is TRANSACTION start
    time, so every row written in one transaction shares it to the microsecond.
    That is the normal case here, not a rarity: task 7's `approve()` writes the
    APPROVED row and publishes `application_approved`, whose 3.10a handler runs
    in the SAME transaction and writes INVOICED beside it — sorted on the
    timestamp alone the two would render in arbitrary order and the timeline
    would say the invoice preceded the approval. `id` is `uuid7`, hence
    time-ordered, and `ix_application_status_history_timeline` carries all three
    columns, so the tie-break is free.
    """
    rows = await db.execute(
        select(ApplicationStatusHistory)
        .where(ApplicationStatusHistory.application_id == application_id)
        .order_by(ApplicationStatusHistory.occurred_at, ApplicationStatusHistory.id)
    )
    return list(rows.scalars().all())


async def list_assignments(
    db: AsyncSession, application_id: uuid.UUID
) -> list[ApplicationAssignment]:
    """Every assignment the application has ever had, oldest first — the
    superseded ones included, because the register is the record of who held it
    when. Same `(created_at, id)` tie-break as the history above and for the
    identical reason: task 7's forward supersedes the reviewer's row and inserts
    the parent organization's in ONE transaction, so both carry the same
    `created_at`."""
    rows = await db.execute(
        select(ApplicationAssignment)
        .where(ApplicationAssignment.application_id == application_id)
        .order_by(ApplicationAssignment.created_at, ApplicationAssignment.id)
    )
    return list(rows.scalars().all())


async def deactivate_assignments(db: AsyncSession, application_id: uuid.UUID) -> None:
    """Clear the application's ACTIVE assignment, if it has one — the first half
    of a supersede write, whose second half is `add_assignment` below.

    `uq_application_assignments_active` is UNIQUE on `(application_id) WHERE
    is_active`, so the two halves must not be pending at the same time: the
    index is checked at flush and a still-true old row makes the insert an
    `IntegrityError` on a conflict the flush order would have resolved (lesson:
    "A partial unique index constrains only the rows it covers, and only after a
    flush"). This is a Core UPDATE and therefore hits the database immediately;
    `service._claim_assignment` flushes between the two all the same, so the
    ordering is visible where it matters rather than resting on that fact."""
    await db.execute(
        update(ApplicationAssignment)
        .where(
            ApplicationAssignment.application_id == application_id,
            ApplicationAssignment.is_active.is_(True),
        )
        .values(is_active=False)
    )


async def add_assignment(db: AsyncSession, row: ApplicationAssignment) -> None:
    """Stage the new assignment and flush, so the partial unique index above
    surfaces at the call site rather than at the end of the request. Mirrors
    `add_status_history`."""
    db.add(row)
    await db.flush()


# --- Task 1 (3.9b): auto-assignment on submission ------------------------------


async def get_active_assignment(
    db: AsyncSession, application_id: uuid.UUID
) -> ApplicationAssignment | None:
    """The application's current assignment row, if it has one — the read half
    of the claim/supersede decision `service._claim_assignment` makes (ruling
    16.2), and the guard `submit`'s auto-assignment hook checks before running
    at all (ruling 6: a resubmission must find one and skip)."""
    rows = await db.execute(
        select(ApplicationAssignment).where(
            ApplicationAssignment.application_id == application_id,
            ApplicationAssignment.is_active.is_(True),
        )
    )
    return rows.scalars().first()


async def review_candidates(
    db: AsyncSession, eligible_user_ids: Sequence[uuid.UUID]
) -> list[Candidate]:
    """One `Candidate` per id in `eligible_user_ids`, carrying how many
    applications each currently holds as an ACTIVE assignment.

    WHO is eligible is `auth`'s question — a permission lookup the caller
    (`service._auto_assign_on_submission`) resolves and hands down, the same
    way `list_applications` receives `contour_organization_col` rather than
    reaching into `gis` itself (review I2: this file imports no other
    module's service). HOW LOADED each one already is, is this module's own
    `application_assignments` table. Every id comes back — zero-count ones
    included — so `assignment.choose_executor` sees the WHOLE pool ruling 7
    asks it to tie-break over, not just the ones with an existing row.
    """
    if not eligible_user_ids:
        return []
    rows = await db.execute(
        select(ApplicationAssignment.user_id, func.count())
        .where(
            ApplicationAssignment.user_id.in_(eligible_user_ids),
            ApplicationAssignment.is_active.is_(True),
        )
        .group_by(ApplicationAssignment.user_id)
    )
    open_counts = {user_id: count for user_id, count in rows.all()}
    return [
        Candidate(user_id=user_id, open_count=open_counts.get(user_id, 0))
        for user_id in eligible_user_ids
    ]


# --- Task 2: the SLA sweep's own two candidate sets --------------------------
#
# Both mirror `payments.repo.list_invoices_due_soon`/`list_refunds_past_due`
# exactly: a status filter alone is what makes a second sweep run a no-op for
# a row the first one already moved past this query's own WHERE clause
# (decided, or paused into `PENDING_INFO`). `sla.SLA_ACTIVE_STATUSES` is the
# same tuple `sla.is_overdue` reads — one source for "is the clock even
# running" — and deliberately NOT this file's own `ACTIVE_STATUSES` above
# (migration 0015's duplicate-guard set, a different question entirely: an
# APPROVED/INVOICED/PAID application still occupies its plot, but its SLA
# clock has already stopped).


async def list_applications_sla_due_soon(
    db: AsyncSession, *, now: datetime, before: datetime
) -> Sequence[Application]:
    """Every SLA-active application due in `[now, before]` — not yet overdue
    (`list_applications_past_sla_deadline`'s own set) but inside
    `applications.jobs.sla_sweep`'s reminder window."""
    stmt = select(Application).where(
        Application.status.in_(SLA_ACTIVE_STATUSES),
        Application.sla_deadline_at >= now,
        Application.sla_deadline_at <= before,
    )
    return (await db.execute(stmt)).scalars().all()


async def list_applications_past_sla_deadline(
    db: AsyncSession, *, now: datetime
) -> Sequence[Application]:
    """Every SLA-active application whose deadline has already passed —
    `applications.jobs.sla_sweep`'s candidate set for RI-07."""
    stmt = select(Application).where(
        Application.status.in_(SLA_ACTIVE_STATUSES), Application.sla_deadline_at < now
    )
    return (await db.execute(stmt)).scalars().all()


# --- Task 4: the request for information --------------------------------------


async def get_open_info_request(db: AsyncSession, application_id: uuid.UUID) -> InfoRequest | None:
    """The newest OPEN (`responded_at IS NULL`) `info_requests` row for this
    application, or `None`.

    `service.request_info` reads this as its own 409 guard ("a second open
    request while one is already open" — ruling 8's pause arithmetic has no
    way to tell which `responded_at` closes which `requested_at` once two are
    open at once), and `service.respond_info` reads it as the row to close.
    Both callers already hold the application's own row lock
    (`get_application_for_update`), so this needs none of its own: two
    concurrent calls on the same application serialise on THAT lock first."""
    stmt = (
        select(InfoRequest)
        .where(InfoRequest.application_id == application_id, InfoRequest.responded_at.is_(None))
        .order_by(InfoRequest.requested_at.desc(), InfoRequest.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def add_info_request(db: AsyncSession, info_request: InfoRequest) -> None:
    """Stage and flush — mirrors `add_status_history`'s own shape, so the
    caller's other pending writes in the same transaction (the `applications`
    status UPDATE) surface together with this INSERT."""
    db.add(info_request)
    await db.flush()


async def list_info_requests(db: AsyncSession, application_id: uuid.UUID) -> Sequence[InfoRequest]:
    """Every `info_requests` row this application has ever had, oldest first —
    open or closed alike, the same "the register is the record of who held it
    when" reasoning `list_assignments` states for its own superseded rows.
    `service.timeline`'s own consumer (final whole-branch review, IMPORTANT):
    the pause is the one event on this branch that silently moves a
    legally-consequential deadline, and it belongs in the only audit view an
    inspector reads."""
    stmt = (
        select(InfoRequest)
        .where(InfoRequest.application_id == application_id)
        .order_by(InfoRequest.requested_at, InfoRequest.id)
    )
    return (await db.execute(stmt)).scalars().all()
