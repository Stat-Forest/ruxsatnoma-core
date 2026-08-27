"""Audit module models: the append-only audit_log journal (design/02 § audit)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IPAddressString, uuid7


class AuditLog(Base):
    """One action trail entry. INSERT-only: UPDATE/DELETE/TRUNCATE are
    rejected by DB triggers (migration 0002); an attempt is incident RI-06.

    Retention >= 3 years; partitioning deferred until real volumes
    (design/02). `user_id` FK to users added in migration 0003 (stage 3.2).

    - `occurred_at` is transaction-start time (`now()`): rows written in the
      same transaction share one value, so it does not by itself total-order
      events across rows — order by `(occurred_at, id)`, since `id` is a
      monotonic UUIDv7.
    - The FK `user_id -> users` (migration 0003) is NO ACTION (ruling 3) and
      was added NOT VALID + VALIDATE CONSTRAINT: ON DELETE SET NULL/CASCADE
      would fire the append-only triggers above, so users are never
      physically deleted (block/soft-delete only).
    - Retention >= 3 years; rows can never be deleted here. Future purging
      happens via partition DETACH+DROP, never TRUNCATE (triggers forbid it).
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # Transaction-start time: the trail carries the exact time of the action
    # it is written together with (same transaction).
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id")
    )  # null = system/worker
    action: Mapped[str]  # "<object>.<verb>", e.g. "application.submit"
    object_type: Mapped[str | None]
    object_id: Mapped[uuid.UUID | None]
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    basis: Mapped[str | None]
    ip: Mapped[str | None] = mapped_column(IPAddressString)
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
