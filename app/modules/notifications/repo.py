"""SQL for notification templates and notifications. No business rules here."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.notifications.models import Notification, NotificationTemplate


async def get_active_template(
    db: AsyncSession, *, event_code: str, channel: str
) -> NotificationTemplate | None:
    return (
        await db.execute(
            select(NotificationTemplate).where(
                NotificationTemplate.event_code == event_code,
                NotificationTemplate.channel == channel,
                NotificationTemplate.status == "active",
            )
        )
    ).scalar_one_or_none()


async def get_template(db: AsyncSession, template_id: uuid.UUID) -> NotificationTemplate | None:
    return await db.get(NotificationTemplate, template_id)


async def max_version(db: AsyncSession, *, event_code: str, channel: str) -> int:
    value = (
        await db.execute(
            select(func.max(NotificationTemplate.version)).where(
                NotificationTemplate.event_code == event_code,
                NotificationTemplate.channel == channel,
            )
        )
    ).scalar_one_or_none()
    return value or 0


async def list_templates(
    db: AsyncSession,
    *,
    event_code: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    page: int,
    page_size: int,
) -> tuple[list[NotificationTemplate], int]:
    stmt = select(NotificationTemplate)
    if event_code is not None:
        stmt = stmt.where(NotificationTemplate.event_code == event_code)
    if channel is not None:
        stmt = stmt.where(NotificationTemplate.channel == channel)
    if status is not None:
        stmt = stmt.where(NotificationTemplate.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    NotificationTemplate.event_code,
                    NotificationTemplate.channel,
                    NotificationTemplate.version.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def add(db: AsyncSession, row: NotificationTemplate | Notification) -> None:
    db.add(row)
    await db.flush()
