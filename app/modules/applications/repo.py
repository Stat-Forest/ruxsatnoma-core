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
(CLAUDE.md); the CONTOUR half of that same JOIN is not ours to build, so it
comes from `gis.service.contour_organization_column`."""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.applications.models import (
    Application,
    ApplicationCheck,
    ApplicationDocument,
    ApplicationItem,
    ApplicationStatusHistory,
)
from app.modules.gis import service as gis_service


async def get_application(db: AsyncSession, application_id: uuid.UUID) -> Application | None:
    return await db.get(Application, application_id)


async def get_application_for_update(
    db: AsyncSession, application_id: uuid.UUID
) -> Application | None:
    """`service.get`'s locking sibling — `set_status` ONLY (review C1).
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
    """The attachments, oldest first. Empty for every application until task 4
    ships the upload route."""
    rows = await db.execute(
        select(ApplicationDocument)
        .where(ApplicationDocument.application_id == application_id)
        .order_by(ApplicationDocument.id)
    )
    return list(rows.scalars().all())


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


def _zone_join_target() -> Any:
    """The organization an application's zone rule compares against:
    `assigned_org_id` while it has one, the contour's owner before a reviewer
    takes it into work.

    `assigned_org_id` is null for every DRAFT and stays null through SUBMITTED
    until `start-review` writes the assignment (plan ruling 14), so a zone rule
    reading that column alone would show a hodim an EMPTY work queue — the
    applications they are supposed to pick up are precisely the unassigned ones.
    The contour half is built by `gis.service.contour_organization_column`, not
    here: `contours` is gis's table, and design/01 rule 5 does not extend to
    this module."""
    return func.coalesce(
        Application.assigned_org_id,
        gis_service.contour_organization_column(Application.contour_id),
    )


async def list_applications(
    db: AsyncSession,
    *,
    scope: Any,
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

    `scope` is whatever the service built out of the caller's identity — an
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

    join_target = _zone_join_target()
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
