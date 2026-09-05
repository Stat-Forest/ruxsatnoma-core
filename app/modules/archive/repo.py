"""`archive`'s own table only. Unlike `search`, this module never reads
another module's table directly: the one thing it needs from `applications`/
`permits` — the row's current status and organization — comes through their
own `service.get()`, the ordinary cross-module door (design/01 rule 2), so
the reader exception (design/01 rule 5) is not even invoked here."""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.archive.models import ArchiveItem


async def add(db: AsyncSession, row: ArchiveItem) -> ArchiveItem:
    db.add(row)
    await db.flush()
    return row


async def item_by_id(db: AsyncSession, item_id: uuid.UUID) -> ArchiveItem | None:
    return await db.get(ArchiveItem, item_id)


async def item_by_id_for_update(db: AsyncSession, item_id: uuid.UUID) -> ArchiveItem | None:
    """Locked read for `verify` — two concurrent verifications of the same
    item must not both flip `status` from stale in-memory copies (the same
    lock-before-write shape every other module's own `set_status` uses)."""
    return (
        await db.execute(select(ArchiveItem).where(ArchiveItem.id == item_id).with_for_update())
    ).scalar_one_or_none()


async def list_items(
    db: AsyncSession,
    *,
    scope: Any,
    object_type: str | None,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[ArchiveItem], int]:
    """One page of `archive_items` matching `scope` (the caller's zone,
    `abac.zone_filter` over `organizations.region_id`/`district_id` joined
    from this table's own `organization_id`) and the given filters, with the
    total — same shape as every other module's own `list_*`
    (`permits.repo.list_permits` is the template). LEFT JOIN: `organization_id`
    is nullable, and a republic-wide actor's `zone_filter` is `true()` and
    must not depend on the join at all."""
    conditions: list[Any] = [scope]
    for column, value in (
        (ArchiveItem.object_type, object_type),
        (ArchiveItem.status, status),
    ):
        if value is not None:
            conditions.append(column == value)
    joined = (
        select(ArchiveItem.id)
        .outerjoin(Organization, Organization.id == ArchiveItem.organization_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(joined.subquery()))).scalar_one()
    rows = await db.execute(
        select(ArchiveItem)
        .outerjoin(Organization, Organization.id == ArchiveItem.organization_id)
        .where(*conditions)
        .order_by(ArchiveItem.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(rows.scalars().all()), total
