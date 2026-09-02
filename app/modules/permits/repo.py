"""Every SQL statement this module issues. No business rules live here — the
service decides, the repo asks.

The one statement worth reading twice is `next_number`: it is the whole of
ruling 9's race-freedom, and its failure mode is silence."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
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
