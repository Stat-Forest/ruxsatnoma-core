"""beekeepers repository: DB access for the certificate-holder register."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.beekeepers.models import Beekeeper


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def get_beekeeper(db: AsyncSession, beekeeper_id: uuid.UUID) -> Beekeeper | None:
    return await db.get(Beekeeper, beekeeper_id)


async def get_active_by_certificate_no(db: AsyncSession, certificate_no: str) -> Beekeeper | None:
    """Case/whitespace-insensitive match among `status='active'` rows only.

    Shared by `service.create_beekeeper`'s duplicate pre-check and `service.
    match_certificate`'s filing-time lookup — one normalization, so the two
    can never disagree about what counts as "the same number". The DB's own
    partial unique index (`uq_beekeepers_certificate_no_active`) is exact-match
    only and does not itself catch a casing variant; this function is what
    makes the softer rule real before a write, and what the applicant's typed
    number is compared against at filing time.
    """
    needle = certificate_no.strip().upper()
    stmt = select(Beekeeper).where(
        Beekeeper.status == "active",
        func.upper(func.trim(Beekeeper.certificate_no)) == needle,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_beekeepers(
    db: AsyncSession,
    *,
    q: str | None,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[Beekeeper], int]:
    """`q` is an ILIKE substring search across certificate_no/full_name/pinfl
    (the plan's own three fields); `status` narrows to `active`/`removed`.
    Filters combine with AND, the same convention `auth.repo.list_users` uses."""
    stmt = select(Beekeeper)
    if status is not None:
        stmt = stmt.where(Beekeeper.status == status)
    if q is not None:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Beekeeper.certificate_no.ilike(pattern),
                Beekeeper.full_name.ilike(pattern),
                Beekeeper.pinfl.ilike(pattern),
            )
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Beekeeper.created_at.desc(), Beekeeper.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return list(rows), total
