"""Audit module models: the append-only audit_log journal (design/02 § audit)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7


class AuditLog(Base):
    """One action trail entry. INSERT-only: UPDATE/DELETE/TRUNCATE are
    rejected by DB triggers (migration 0002); an attempt is incident RI-06.

    Retention >= 3 years; partitioning deferred until real volumes
    (design/02). `user_id` gains its FK to users in stage 3.2.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # Transaction-start time: the trail carries the exact time of the action
    # it is written together with (same transaction).
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_id: Mapped[uuid.UUID | None]  # null = system/worker; FK added in 3.2
    action: Mapped[str]  # "<object>.<verb>", e.g. "application.submit"
    object_type: Mapped[str | None]
    object_id: Mapped[uuid.UUID | None]
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    basis: Mapped[str | None]
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None]
    # text, not uuid: client-supplied X-Request-Id passes through as-is
    correlation_id: Mapped[str | None]
    result: Mapped[str] = mapped_column(default="success")
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __mapper_args__ = {"eager_defaults": True}  # occurred_at available right after flush

    __table_args__ = (
        CheckConstraint("result IN ('success', 'denied', 'error')", name="result_valid"),
        Index("ix_audit_log_object", "object_type", "object_id", "occurred_at"),
        Index("ix_audit_log_user", "user_id", "occurred_at"),
        Index("ix_audit_log_occurred_at_brin", "occurred_at", postgresql_using="brin"),
    )
