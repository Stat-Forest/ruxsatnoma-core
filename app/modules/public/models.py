"""public — module 10.11: open data, aggregate GIS layers, citizens' enquiries
(design/02 § public, plan `04.6-4.8-public-help.md`).

Only one table lives here: `qr_check_log` is filed under `## public` in
`design/02` but is created and written by `permits` (3.11a ruling 15) — this
module's own permit check never touches it. `citizen_appeals` is genuinely
this module's, the first one built here.

**Its content is written by the public, most of it with no authentication at
all** (ruling R5, `plans/04.6-4.8-public-help.md`) — `subject`/`body`/
`answer_text`/`contact` are untrusted strings and must never be interpolated
into a raised exception, a log line, or any other admin-visible field outside
this row itself.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

APPEAL_NUMBER_PREFIX = "MR"
APPEAL_STATUSES = ("new", "in_progress", "answered", "closed")
# Every edge a staff transition may take (`public.service.advance_appeal_status`
# and `answer_appeal`). `answered` is reached ONLY through `answer_appeal`
# (never the bare status-advance route), which is what guarantees an
# `answered` row always carries `answer_text`/`answered_by`/`answered_at`.
# A `new` appeal may be answered directly (staff often reads and answers in
# one sitting) or moved to `in_progress` first when it needs work before an
# answer exists.
APPEAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("in_progress", "answered", "closed"),
    "in_progress": ("answered", "closed"),
    "answered": ("closed",),
    "closed": (),
}


class CitizenAppeal(Base):
    """An обращение (`tz/02` module 10.11, С27) — anonymous by design (`tz/04`:
    "любой, без авторизации"). `number` comes from the same gapless
    `number_counters` scheme every other public number in this codebase uses
    (`MR-{YEAR}-{NUMBER6}`, design/03's own table), so it is a WALKABLE space —
    `public.service.check_appeal_status` never answers by number alone (R3)."""

    __tablename__ = "citizen_appeals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str] = mapped_column(unique=True)
    applicant_name: Mapped[str]
    # `{"phone": "...", "email": "..."}` — at least one key required, enforced
    # in the service (a CHECK on JSONB key presence is possible but reads far
    # worse than the one-line service guard, and this is not security-load-
    # bearing the way a CHECK earns its keep elsewhere in this codebase).
    contact: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str]
    body: Mapped[str]
    # Stays NULL this stage (ruling R4, `plans/04.6-4.8-public-help.md`):
    # `POST /files` requires `get_current_user`, and an anonymous citizen has
    # no session — the column exists so a later stage can wire an upload path
    # without a migration, the same "reserved, not yet written" shape
    # `forest_tickets.file_id` used in 3.11a.
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    status: Mapped[str] = mapped_column(default="new")
    answer_text: Mapped[str | None]
    answered_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    answered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {APPEAL_STATUSES}", name="status_valid"),
        # `answered_at`/`answer_text` travel together: a row entering `answered`
        # without going through `answer_appeal` (the only writer of both) is a
        # bug the CHECK catches instead of the front-end silently rendering an
        # empty answer as if the appeal had none.
        CheckConstraint(
            "(status = 'answered') = (answer_text IS NOT NULL AND answered_at IS NOT NULL)",
            name="answered_fields_consistent",
        ),
        Index("ix_citizen_appeals_status", "status"),
    )
