"""SQL for notification templates and notifications. No business rules here."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
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


async def get_notification(db: AsyncSession, notification_id: uuid.UUID) -> Notification | None:
    return await db.get(Notification, notification_id)


async def get_by_provider_message_id(
    db: AsyncSession, provider_message_id: str
) -> Notification | None:
    return (
        await db.execute(
            select(Notification)
            .where(Notification.provider_message_id == provider_message_id)
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def notification_exists(
    db: AsyncSession, *, event_code: str, object_id: uuid.UUID, channel: str
) -> bool:
    """Whether at least one `channel` notification for `event_code`/`object_id`
    already exists — a periodic job's own once-only check before calling
    `notify()` again for the same object (payments.jobs.expiry_sweep, task 6)."""
    stmt = (
        select(Notification.id)
        .where(
            Notification.event_code == event_code,
            Notification.object_id == object_id,
            Notification.channel == channel,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


def _inbox_stmt(user_id: uuid.UUID, unread_only: bool):
    stmt = select(Notification).where(
        Notification.recipient_user_id == user_id, Notification.channel == "inapp"
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    return stmt


async def list_inbox(
    db: AsyncSession, *, user_id: uuid.UUID, unread_only: bool, page: int, page_size: int
) -> tuple[list[Notification], int]:
    stmt = _inbox_stmt(user_id, unread_only)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(Notification.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(_inbox_stmt(user_id, unread_only=True).subquery())
        )
    ).scalar_one()


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        update(Notification)
        .where(
            Notification.recipient_user_id == user_id,
            Notification.channel == "inapp",
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    return result.rowcount  # pyright: ignore[reportAttributeAccessIssue]
