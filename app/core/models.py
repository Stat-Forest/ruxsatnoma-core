"""Infrastructure models owned by core: no business logic, no domain vocabulary.

`system_settings` lives here (stage 3.3a ruling 8) so that level-1 `auth` can read
session/lockout policy without importing level-1 `admin` (that would be a cycle:
`admin` already calls `auth`). Writes go through `admin.service.update_setting`.
Later inhabitants of this file: `media_files`, `idempotency_keys`, `number_counters`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


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
