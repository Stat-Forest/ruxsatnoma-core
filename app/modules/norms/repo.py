"""Queries for norms, tariffs, parameters and calculations. Nothing here decides
anything — an "effective" row is one that is published and whose period contains
the date, and that is a fact, not a policy."""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import RowMapping, Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.norms.models import Calculation, Norm, RuleParameter, Tariff


def _in_force(model: type[RuleParameter] | type[Tariff] | type[Norm], on_date: date):
    return (
        model.status == "published",
        model.effective_from <= on_date,
        or_(model.effective_to.is_(None), model.effective_to >= on_date),
    )


async def effective_parameters(
    db: AsyncSession, codes: Sequence[str], on_date: date
) -> dict[str, RuleParameter]:
    """Every requested code that has a published row in force on `on_date`.
    A code with no row is simply absent from the mapping — the caller decides
    whether that is fatal (the calculator raises ERR-NORM-004; a lister does not)."""
    rows = await db.execute(
        select(RuleParameter).where(
            RuleParameter.code.in_(list(codes)), *_in_force(RuleParameter, on_date)
        )
    )
    return {row.code: row for row in rows.scalars()}


async def effective_parameters_by_prefix(
    db: AsyncSession, prefix: str, on_date: date
) -> dict[str, RuleParameter]:
    """The `coef_sb:` and `tariff_group:` families, fetched in one query rather
    than ten (the calculator needs every livestock type the request mentions)."""
    rows = await db.execute(
        select(RuleParameter).where(
            RuleParameter.code.startswith(prefix), *_in_force(RuleParameter, on_date)
        )
    )
    return {row.code: row for row in rows.scalars()}


async def effective_tariffs(
    db: AsyncSession, activity_type_id: uuid.UUID, on_date: date
) -> list[Tariff]:
    rows = await db.execute(
        select(Tariff).where(
            Tariff.activity_type_id == activity_type_id, *_in_force(Tariff, on_date)
        )
    )
    return list(rows.scalars())


async def effective_norm(
    db: AsyncSession, contour_id: uuid.UUID, activity_type_id: uuid.UUID, on_date: date
) -> Norm | None:
    rows = await db.execute(
        select(Norm).where(
            Norm.contour_id == contour_id,
            Norm.activity_type_id == activity_type_id,
            *_in_force(Norm, on_date),
        )
    )
    return rows.scalar_one_or_none()


async def published_overlaps(
    db: AsyncSession,
    model: type[RuleParameter] | type[Tariff] | type[Norm],
    row: RuleParameter | Tariff | Norm,
    key_filters: Sequence[Any],
) -> bool:
    """Does a published row already cover any day of `row`'s period? Asked BEFORE
    publishing so the caller answers 409 instead of letting the EXCLUDE constraint
    surface as a 500 (lesson: IntegrityError IS a DBAPIError)."""
    upper = row.effective_to
    stmt = (
        select(func.count())
        .select_from(model)
        .where(
            model.status == "published",
            model.id != row.id,
            model.effective_from <= (upper if upper is not None else date.max),
            or_(model.effective_to.is_(None), model.effective_to >= row.effective_from),
            *key_filters,
        )
    )
    return bool((await db.execute(stmt)).scalar_one())


async def list_parameters(
    db: AsyncSession, *, code: str | None, status: str | None, limit: int, offset: int
) -> tuple[list[RuleParameter], int]:
    """Every rule parameter matching the given filters, any status — a maker's
    own drafts must show up here too (`test_the_list_filters_by_code_and_pages`
    lists three freshly-created drafts, none of them published)."""
    stmt = select(RuleParameter)
    if code is not None:
        stmt = stmt.where(RuleParameter.code == code)
    if status is not None:
        stmt = stmt.where(RuleParameter.status == status)
    stmt = stmt.order_by(RuleParameter.code, RuleParameter.effective_from)
    return await paginate(db, stmt, limit, offset)


async def list_tariffs(
    db: AsyncSession,
    *,
    activity_type_id: uuid.UUID | None,
    on_date: date | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Tariff], int]:
    """`on_date` answers "what is in force" (published + period contains the
    date) — the router always supplies one, defaulting to `business_today()`,
    since that is this route's whole purpose. When `status` is given
    explicitly it is ANDed in as its own filter; when it is not, and a date
    was given, "in force" implies `status = 'published'` on its own."""
    stmt = select(Tariff)
    if activity_type_id is not None:
        stmt = stmt.where(Tariff.activity_type_id == activity_type_id)
    if status is not None:
        stmt = stmt.where(Tariff.status == status)
    if on_date is not None:
        stmt = stmt.where(
            Tariff.effective_from <= on_date,
            or_(Tariff.effective_to.is_(None), Tariff.effective_to >= on_date),
        )
        if status is None:
            stmt = stmt.where(Tariff.status == "published")
    stmt = stmt.order_by(Tariff.effective_from, Tariff.livestock_group)
    return await paginate(db, stmt, limit, offset)


async def list_norms(
    db: AsyncSession,
    *,
    contour_id: uuid.UUID | None,
    activity_type_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Norm], int]:
    """Every norm matching the given filters, any status — a specialist's own
    drafts must show up here too, the same reasoning `list_parameters`
    documents for rule parameters."""
    stmt = select(Norm)
    if contour_id is not None:
        stmt = stmt.where(Norm.contour_id == contour_id)
    if activity_type_id is not None:
        stmt = stmt.where(Norm.activity_type_id == activity_type_id)
    if status is not None:
        stmt = stmt.where(Norm.status == status)
    stmt = stmt.order_by(Norm.effective_from, Norm.id)
    return await paginate(db, stmt, limit, offset)


def _calculations_query(application_id: uuid.UUID | None, created_by: uuid.UUID | None) -> Select:
    """The WHERE + ORDER BY `list_calculations` and `newest_calculation`
    share, NEWEST first — the one ordering in this module not by
    `effective_from`, since a calculation has no period of its own. `id`
    breaks a `created_at` tie: `uuid7` is time-ordered, so it agrees with
    insertion order even when two saves land in the same tick."""
    stmt = select(Calculation)
    if application_id is not None:
        stmt = stmt.where(Calculation.application_id == application_id)
    if created_by is not None:
        stmt = stmt.where(Calculation.created_by == created_by)
    return stmt.order_by(Calculation.created_at.desc(), Calculation.id.desc())


async def list_calculations(
    db: AsyncSession,
    *,
    application_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[Calculation], int]:
    """An append-only table's history, paged, with its total (review M3:
    the `COUNT(*)` a page needs and `newest_calculation` below does not).

    `created_by` is ruling 11's own-rows scope, and it is keyword-only with NO
    default on purpose: this table holds every fee the system has ever quoted,
    and an unscoped read of it is the defect this stage exists to close. The
    caller — `service.list_calculations`, the only one — has to say `None`
    deliberately, which it does exactly twice: for a named application whose
    entitlement it already checked, and for the superuser.
    """
    return await paginate(db, _calculations_query(application_id, created_by), limit, offset)


async def newest_calculation(db: AsyncSession, application_id: uuid.UUID) -> Calculation | None:
    """The single newest row for `application_id`, or `None` — `LIMIT 1` off
    the same ordering as `list_calculations`, without `paginate`'s
    `COUNT(*)` (review M3: `norms.service.latest_calculation` runs this once
    per invoice build in 3.10, and the total is never used there).

    `created_by=None` on purpose and not by default: this is the IN-PROCESS
    read 3.10a builds an invoice from, where the caller is another service and
    ruling 11's scope — an HTTP rule about a signed-in human — does not
    apply."""
    stmt = _calculations_query(application_id, created_by=None).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def paginate(db: AsyncSession, stmt: Select, limit: int, offset: int) -> tuple[list, int]:
    """`Page`-shaped result: the window plus the unwindowed total."""
    total = (
        await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()
    rows = await db.execute(stmt.limit(limit).offset(offset))
    return list(rows.scalars()), total


# --- Ruling 20 (plan 03.9a): the ONE read of another module's table ----------
#
# `norms` is level 2 and `applications` is level 3, so `norms` may NOT call
# `applications.service` to ask whose application a calculation belongs to.
# **Ruling 20 grants `norms` a read-only right on the `applications` table for
# this one predicate, in THIS FILE and nowhere else.**
#
# This is a NEW exception that AMENDS design/01 rule 5 — it is not an instance
# of it. Rule 5's targeted addition reads, verbatim: "`gis` and `norms` get the
# same read-only right on the `permits` table **(and only on it)**". The
# parenthesis is the whole point of that sentence: it exists to stop the
# exception spreading. Extending it to `applications` therefore had to be
# recorded as its own ruling, and Task 9 edits design/01 to say so.
#
# The scope is a HARD LIMIT, not an example: `id`, `applicant_id`,
# `assigned_org_id`, `contour_id` (ruling 20's own four) plus `status`
# (controller ruling R15, added when the status guard below became this
# stage's obligation — ruling 20's column list was written for the ownership
# predicate alone, and the guard cannot be written without it). Any WRITE, and
# any read from `norms/service.py`, is still forbidden.
#
# Raw SQL naming the five columns rather than an ORM query: importing
# `applications.models` here would be a level-3 import from a level-2 module —
# the very thing the ruling was needed to avoid — and the explicit column list
# is the limit above, written where it is enforced rather than only promised.
_APPLICATION_FACTS_SQL = text(
    "SELECT id, applicant_id, assigned_org_id, contour_id, status "
    "FROM applications WHERE id = :application_id"
)


async def application_facts(db: AsyncSession, application_id: uuid.UUID) -> RowMapping | None:
    """The five columns of ruling 20 (as amended by R15) for one application,
    or `None` when no such application exists.

    Read-only, and the only place in `norms` that touches this table.
    `norms.service._assert_application_open_for_calculation` is its only
    caller; a second caller wanting a sixth column is a sign the ruling needs
    amending again, not that this query does.
    """
    rows = await db.execute(_APPLICATION_FACTS_SQL, {"application_id": application_id})
    return rows.mappings().first()
