"""Queries for norms, tariffs, parameters and calculations. Nothing here decides
anything — an "effective" row is one that is published and whose period contains
the date, and that is a fact, not a policy."""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Select, func, or_, select
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


def _calculations_query(application_id: uuid.UUID | None) -> Select:
    """The WHERE + ORDER BY `list_calculations` and `newest_calculation`
    share, NEWEST first — the one ordering in this module not by
    `effective_from`, since a calculation has no period of its own. `id`
    breaks a `created_at` tie: `uuid7` is time-ordered, so it agrees with
    insertion order even when two saves land in the same tick."""
    stmt = select(Calculation)
    if application_id is not None:
        stmt = stmt.where(Calculation.application_id == application_id)
    return stmt.order_by(Calculation.created_at.desc(), Calculation.id.desc())


async def list_calculations(
    db: AsyncSession, *, application_id: uuid.UUID | None, limit: int, offset: int
) -> tuple[list[Calculation], int]:
    """An append-only table's history, paged, with its total (review M3:
    the `COUNT(*)` a page needs and `newest_calculation` below does not)."""
    return await paginate(db, _calculations_query(application_id), limit, offset)


async def newest_calculation(db: AsyncSession, application_id: uuid.UUID) -> Calculation | None:
    """The single newest row for `application_id`, or `None` — `LIMIT 1` off
    the same ordering as `list_calculations`, without `paginate`'s
    `COUNT(*)` (review M3: `norms.service.latest_calculation` runs this once
    per invoice build in 3.10, and the total is never used there)."""
    stmt = _calculations_query(application_id).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def paginate(db: AsyncSession, stmt: Select, limit: int, offset: int) -> tuple[list, int]:
    """`Page`-shaped result: the window plus the unwindowed total."""
    total = (
        await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()
    rows = await db.execute(stmt.limit(limit).offset(offset))
    return list(rows.scalars()), total
