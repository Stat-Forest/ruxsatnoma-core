"""Notifications: template management, rendering, and the notify() entry point
upper modules call inside their own transaction (plan 03.5 ruling 4)."""

import re
import uuid
from collections.abc import Mapping
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.notifications import repo
from app.modules.notifications.models import NotificationTemplate
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
