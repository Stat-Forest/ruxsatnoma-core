"""Reliability tables (design/02 § integrations): the outbox, inbound dead
letters, and the per-message integration log. Level 0 — no domain imports."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7


class OutboxMessage(Base):
    """Guaranteed outbound delivery (tz/09): written in the business action's
    transaction, delivered by the worker. `delivering` is reserved in the CHECK
    for a future claim-commit model; the current worker holds the claim as an
    open transaction instead and never writes it (plan 03.4 ruling 5)."""

    __tablename__ = "outbox_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    destination: Mapped[str]  # sender registry key: 'sms_otp' now; 'sms', 'rn_event', ... later
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    correlation_id: Mapped[str | None]  # text, like audit_log (decision #38 ruling 1)
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    delivered_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'dead')", name="status_valid"
        ),
        Index("ix_outbox_messages_due", "status", "next_attempt_at"),
    )


class InboundDeadLetter(Base):
    """Inbound messages that failed schema validation (tz/09). Producers arrive
    with 3.5 (Eskiz callbacks) and 3.10 (Payme); the table and admin view land
    here so the mechanism is complete."""

    __tablename__ = "inbound_dead_letters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    source: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error: Mapped[str]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str] = mapped_column(default="new")
    processed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    processed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("status IN ('new', 'reprocessed', 'discarded')", name="status_valid"),
    )


class IntegrationLog(Base):
    """One row per external message/attempt (tz/09 'logging per message').
    No secrets and no personal data in meta; checksum instead of the body."""

    __tablename__ = "integration_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    direction: Mapped[str]
    system: Mapped[str]
    endpoint: Mapped[str]
    correlation_id: Mapped[str | None]
    idempotency_key: Mapped[str | None]
    http_status: Mapped[int | None]
    checksum: Mapped[str | None]  # sha256 of the payload/body
    duration_ms: Mapped[int | None]
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="direction_valid"),
        Index("ix_integration_log_occurred_at", "occurred_at", postgresql_using="brin"),
    )
