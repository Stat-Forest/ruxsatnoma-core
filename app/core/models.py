"""Infrastructure models owned by core: no business logic, no domain vocabulary.

`system_settings` lives here (stage 3.3a ruling 8) so that level-1 `auth` can read
session/lockout policy without importing level-1 `admin` (that would be a cycle:
`admin` already calls `auth`). Writes go through `admin.service.update_setting`.
`number_counters` (stage 3.9a) is the race-free source of the public numbers
(RX/INV/VC/MR/ST/ChT) — issuing itself is `core/numbers.py`, a later task.
"""

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7


class SystemSetting(Base):
    """One row per OVERRIDDEN runtime parameter; defaults live in settings_store.SETTING_SPECS.

    A missing row is normal, not an error — it means "use the code default".
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    description: Mapped[str | None]
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MediaFile(Base):
    """Every file in the system (design/02 § core): announcements attachments, poa
    PDFs, later permit PDFs and inspection photos. Files are never deleted
    (status='archived'); bytes live in MinIO under storage_key."""

    __tablename__ = "media_files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    storage_key: Mapped[str] = mapped_column(unique=True)
    filename: Mapped[str]
    content_type: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str]
    taken_at: Mapped[datetime | None]
    gps: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326))
    device: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="status_valid"),
        Index("ix_media_files_uploaded_by", "uploaded_by"),
    )


class IdempotencyKey(Base):
    """Idempotency-Key on critical POSTs (design/03): the marker row is inserted
    and committed before the handler runs; the response is stored by
    `IdempotencyContext.save`. In-flight markers older than 5 minutes are
    re-claimable (plan 03.4 ruling 13); completed rows are purged after
    `purge_idempotency_after_hours`."""

    __tablename__ = "idempotency_keys"

    key: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    fingerprint: Mapped[str]  # sha256 of "METHOD|path|body"
    route: Mapped[str]
    response_status: Mapped[int | None]
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NumberCounter(Base):
    """One row per (prefix:year) scope — the public-number series (RX/INV/VC/MR/ST/ChT;
    permit series have their own `permit_counters` in `permits`). No `id`/`created_at`
    — `scope` is itself the natural key, the same shape as `SystemSetting` above.

    `app.core.numbers.next_public_number` is the ONLY writer, and it is not an
    `UPDATE ... RETURNING`: it does `INSERT ... ON CONFLICT DO NOTHING` to create the
    year's row, then `SELECT ... FOR UPDATE` and increments in Python, inside the
    caller's transaction (plan 03.9a ruling 5а). Both shapes are race-free, so the
    difference is not safety — it is that the lock is held to the caller's COMMIT, so
    a submission that fails afterwards rolls its number back and the year's numbering
    has no holes. Do not hand-roll the `UPDATE ... RETURNING` beside it: it would
    escape the shared scope-key convention and lose that rollback-reuse property."""

    __tablename__ = "number_counters"

    scope: Mapped[str] = mapped_column(primary_key=True)
    last_value: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
