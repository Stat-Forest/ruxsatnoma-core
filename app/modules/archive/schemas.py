"""API shapes for `archive`."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Spelled out rather than `Literal[*models.ARCHIVABLE_OBJECT_TYPES]` — pyright
# rejects a starred variable inside `Literal` (same reason
# `permits.schemas`/`search.schemas` spell out their own status literals).
ArchivableObjectType = Literal["application", "permit"]
ArchiveItemStatus = Literal["stored", "verified"]


class ArchiveRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_until: date | None = None


class ArchiveItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    object_type: ArchivableObjectType
    object_id: uuid.UUID
    organization_id: uuid.UUID | None
    archived_at: datetime
    retention_until: date | None
    content_hash: str
    storage_ref: str
    status: ArchiveItemStatus
    created_by: uuid.UUID | None
