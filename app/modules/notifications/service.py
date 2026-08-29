"""Notifications: template management, rendering, and the notify() entry point
upper modules call inside their own transaction (plan 03.5 ruling 4)."""

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.email import get_email_sender
from app.modules.integrations.adapters.sms import get_sms_sender
from app.modules.integrations.senders import register_sender
from app.modules.notifications import repo
from app.modules.notifications.models import CHANNELS, Notification, NotificationTemplate
from app.modules.notifications.schemas import TemplateIn

logger = structlog.get_logger(__name__)

FALLBACK_LANGUAGE = "uz_cyrl"
# Deliberately narrower than str.format: only {snake_case}. An admin-authored
# template must not be able to reach attributes ({x.__class__}) or indexes,
# and a missing key must not raise inside a business transaction (ruling 9).
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def render(body: dict[str, Any], params: Mapping[str, Any], language: str) -> str:
    text = body.get(language) or body.get(FALLBACK_LANGUAGE) or ""

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            logger.warning("notification.placeholder_missing", placeholder=name)
            return match.group(0)
        return str(params[name])

    return _PLACEHOLDER.sub(_substitute, text)


async def create_template(
    db: AsyncSession, data: TemplateIn, *, actor_id: uuid.UUID, ip: str | None
) -> NotificationTemplate:
    """First version of an (event_code, channel) pair. A pair that already has an
    active version must be superseded instead — otherwise the partial unique index
    would surface as a 500 (the 3.3a lesson: never let IntegrityError be the API)."""
    existing = await repo.get_active_template(db, event_code=data.event_code, channel=data.channel)
    if existing is not None:
        raise err(
            "ERR-VAL-001",
            details={"reason": "active_version_exists", "template_id": str(existing.id)},
        )
    row = NotificationTemplate(
        event_code=data.event_code,
        channel=data.channel,
        subject=data.subject.root if data.subject else None,
        body=data.body.root,
        version=await repo.max_version(db, event_code=data.event_code, channel=data.channel) + 1,
        created_by=actor_id,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="notification_template.create",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
        extra={"event_code": row.event_code, "channel": row.channel, "version": row.version},
    )
    return row


async def supersede_template(
    db: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateIn,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> NotificationTemplate:
    """Archive the active version and insert version+1 (ruling 8). The event_code
    and channel may not change — that would be a different template, not a version."""
    old = await repo.get_template(db, template_id)
    if old is None:
        raise err("ERR-SYS-003", details={"template": str(template_id)})
    if old.status != "active":
        raise err("ERR-VAL-001", details={"reason": "already archived"})
    if (data.event_code, data.channel) != (old.event_code, old.channel):
        raise err("ERR-VAL-001", details={"reason": "event_code and channel must match"})
    old.status = "archived"
    await db.flush()  # release the partial unique index before inserting the new active row
    row = NotificationTemplate(
        event_code=old.event_code,
        channel=old.channel,
        subject=data.subject.root if data.subject else None,
        body=data.body.root,
        version=await repo.max_version(db, event_code=old.event_code, channel=old.channel) + 1,
        created_by=actor_id,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="notification_template.supersede",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
        extra={"previous_id": str(old.id), "version": row.version},
    )
    return row


async def archive_template(
    db: AsyncSession, template_id: uuid.UUID, *, actor_id: uuid.UUID, ip: str | None
) -> NotificationTemplate:
    """Turn a channel off for an event: notify() then skips sms/email and falls back
    for inapp (ruling 10)."""
    row = await repo.get_template(db, template_id)
    if row is None:
        raise err("ERR-SYS-003", details={"template": str(template_id)})
    if row.status != "active":
        raise err("ERR-VAL-001", details={"reason": "already archived"})
    row.status = "archived"
    await db.flush()
    # `updated_at` only carries onupdate=func.now() (no client-side default), and
    # this is the first module to return that column in the same request that
    # touched it: after the flush above, SQLAlchemy leaves it expired rather than
    # fetching it via RETURNING, so a bare re-read outside the session's async
    # context raises MissingGreenlet — refresh it explicitly while still awaitable.
    await db.refresh(row)
    await audit.log(
        db,
        action="notification_template.archive",
        user_id=actor_id,
        object_type="notification_template",
        object_id=row.id,
        ip=ip,
    )
    return row


DEFAULT_CHANNELS = ("inapp", "sms")


def _fallback_body(event_code: str, params: Mapping[str, Any]) -> str:
    """Ruling 10: an in-app notification is never lost to a missing template. The
    text is deliberately raw — an administrator seeing it knows a template is due."""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(params.items()))
    return f"{event_code}: {rendered}" if rendered else event_code


async def _transport_allowed(
    db: AsyncSession, *, channel: str, contact: auth_service.NotificationContact
) -> bool:
    if contact.status != "active":
        return False
    if channel == "sms":
        return bool(
            contact.phone
            and contact.phone_verified
            and await settings_store.get_bool(db, "notifications_sms_enabled")
        )
    return bool(contact.email and contact.email_verified)


async def notify(
    db: AsyncSession,
    *,
    event_code: str,
    recipient_user_id: uuid.UUID,
    params: Mapping[str, Any] | None = None,
    channels: Sequence[str] | None = None,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> list[Notification]:
    """Create the notification rows for one event, in the CALLER'S transaction.

    In-app is always written (С19: legally significant notifications reach the
    cabinet even when other channels are off). Transport channels are enqueued on
    the 3.4 outbox and delivered by its worker — nothing is sent from a request.
    """
    values = dict(params or {})
    requested = tuple(channels) if channels is not None else DEFAULT_CHANNELS
    requested = tuple(dict.fromkeys(requested))  # de-dupe, order preserved: never double-send
    unknown = sorted(set(requested) - set(CHANNELS))
    if unknown:  # a caller typo, not user input — fail loudly in dev and in tests
        raise ValueError(f"unknown notification channels: {unknown}")
    contact = await auth_service.get_notification_contact(db, recipient_user_id)
    if contact is None:
        raise ValueError(f"unknown notification recipient: {recipient_user_id}")

    created: list[Notification] = []
    for channel in ("inapp", *(c for c in requested if c != "inapp")):
        template = await repo.get_active_template(db, event_code=event_code, channel=channel)
        if template is None:
            if channel != "inapp":
                continue  # never send an unrendered SMS
            logger.error("notification.template_missing", event_code=event_code, channel=channel)
        if channel != "inapp" and not await _transport_allowed(
            db, channel=channel, contact=contact
        ):
            continue
        row = Notification(
            recipient_user_id=contact.user_id,
            channel=channel,
            event_code=event_code,
            template_id=template.id if template else None,
            params=values,
            language=contact.language,
            subject=(
                render(template.subject, values, contact.language)
                if template is not None and template.subject
                else None
            ),
            rendered_text=(
                render(template.body, values, contact.language)
                if template is not None
                else _fallback_body(event_code, values)
            ),
            object_type=object_type,
            object_id=object_id,
            correlation_id=correlation_id,
        )
        await repo.add(db, row)
        if channel == "inapp":
            row.status = "delivered"
            row.delivered_at = datetime.now(UTC)
        else:
            message = await integrations_service.enqueue(
                db,
                destination=channel,
                payload={"notification_id": str(row.id)},
                correlation_id=correlation_id,
            )
            assert message is not None  # no idempotency key ⇒ always a fresh row
            row.outbox_message_id = message.id
        created.append(row)
    return created


async def list_inbox(
    db: AsyncSession, *, user_id: uuid.UUID, unread_only: bool, page: int, page_size: int
) -> tuple[list[Notification], int]:
    return await repo.list_inbox(
        db, user_id=user_id, unread_only=unread_only, page=page, page_size=page_size
    )


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repo.unread_count(db, user_id)


async def mark_read(
    db: AsyncSession, notification_id: uuid.UUID, *, user_id: uuid.UUID
) -> Notification:
    """Reading someone else's notification must be indistinguishable from reading a
    row that does not exist — a 403 would confirm the id is real."""
    row = await repo.get_notification(db, notification_id)
    if row is None or row.recipient_user_id != user_id or row.channel != "inapp":
        raise err("ERR-SYS-003", details={"notification": str(notification_id)})
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
    return row


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    return await repo.mark_all_read(db, user_id)


async def _deliver(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Outbox sender for the `sms` and `email` destinations (registered below).

    Runs inside the worker's delivery transaction, so the status write and the
    attempt commit together. Raising means "retry"; returning means "done" — which
    is why an unreachable recipient FAILS the notification and returns instead of
    raising: no number of retries will conjure a verified phone.
    """
    row = await db.get(Notification, uuid.UUID(payload["notification_id"]))
    if row is None:
        logger.warning("notification.vanished", notification_id=payload["notification_id"])
        return
    contact = await auth_service.get_notification_contact(db, row.recipient_user_id)
    if contact is None or not await _transport_allowed(db, channel=row.channel, contact=contact):
        row.status = "failed"
        row.error = "recipient is not reachable on this channel"
        return
    if row.channel == "sms":
        assert contact.phone is not None  # _transport_allowed guarantees it
        provider_id = await get_sms_sender().send(
            phone=contact.phone, text=row.rendered_text, reference=str(row.id)
        )
    else:
        assert contact.email is not None
        provider_id = await get_email_sender().send(
            to=contact.email, subject=row.subject or "", text=row.rendered_text
        )
    row.status = "sent"
    row.sent_at = datetime.now(UTC)
    row.provider_message_id = provider_id


register_sender("sms", _deliver)
register_sender("email", _deliver)
