"""Infrastructure models owned by core: no business logic, no domain vocabulary.

`system_settings` lives here (stage 3.3a ruling 8) so that level-1 `auth` can read
session/lockout policy without importing level-1 `admin` (that would be a cycle:
`admin` already calls `auth`). Writes go through `admin.service.update_setting`.
Later inhabitants of this file: `idempotency_keys`, `number_counters`.
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
