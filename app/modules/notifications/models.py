"""Notification templates and the notification record (design/02 § notifications,
corrected by plan 03.5 rulings 5-7). Level 2: this module reads `auth` and `admin`
through their services and never touches their tables directly."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

CHANNELS = ("inapp", "sms", "email")
STATUSES = ("queued", "sent", "delivered", "failed")


class NotificationTemplate(Base):
    """Exactly one ACTIVE version per (event_code, channel); a new text is a
    supersede (archive + insert version+1), never an in-place rewrite, so the
    `rendered_text` snapshot of an old notification stays explainable (ruling 8)."""

    __tablename__ = "notification_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    event_code: Mapped[str]
    channel: Mapped[str]
    subject: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # email only
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("channel IN ('inapp', 'sms', 'email')", name="channel_valid"),
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        CheckConstraint("version > 0", name="version_positive"),
        UniqueConstraint("event_code", "channel", "version", name="uq_version"),
        Index(
            "uq_notification_templates_active",
            "event_code",
            "channel",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class Notification(Base):
    """The business record: one row per channel per event. Retry and backoff live
    in the outbox (ruling 5) — `outbox_message_id` is the link, and it is SET NULL
    when the 3.4 purge job deletes a delivered transport row. In-app rows have no
    transport at all and are born 'delivered' (ruling 7)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str]
    event_code: Mapped[str]
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notification_templates.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    language: Mapped[str]
    subject: Mapped[str | None]
    rendered_text: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued")
    outbox_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("outbox_messages.id", ondelete="SET NULL")
    )
    provider_message_id: Mapped[str | None]
    error: Mapped[str | None]
    object_type: Mapped[str | None]
    object_id: Mapped[uuid.UUID | None]
    correlation_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    read_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("channel IN ('inapp', 'sms', 'email')", name="channel_valid"),
        CheckConstraint("status IN ('queued', 'sent', 'delivered', 'failed')", name="status_valid"),
        Index("ix_notifications_inbox", "recipient_user_id", "created_at"),
        Index(
            "ix_notifications_provider_message_id",
            "provider_message_id",
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
    )
