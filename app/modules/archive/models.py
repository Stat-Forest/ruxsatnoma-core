"""Archive — a level-5 reader (design/01 rule 5, design/02 § archive).

One table, `archive_items`. The write path never touches another module's
row directly: `archive.service.archive_object` moves the source
application/permit to its terminal `ARCHIVED`/`archived` status through THAT
module's own `set_status` (the one way any module outside it may do so), and
writes this table's own row in the same transaction.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# What this module knows how to archive. Each one names a status BOTH the
# owning module's own transition table (`applications.service.
# APPLICATION_TRANSITIONS`, `permits.service.PERMIT_TRANSITIONS`) allows into
# its terminal archived status AND this tuple — `service.py` derives the
# eligible SOURCE statuses from those constants directly rather than
# retyping them, but the object_type itself has no other source, so it lives
# here (one source of truth for the one thing only this module decides: which
# kinds of object it knows how to archive at all).
ARCHIVABLE_OBJECT_TYPES = ("application", "permit")

ARCHIVE_ITEM_STATUSES = ("stored", "verified")


class ArchiveItem(Base):
    """One archived object (design/02 § archive: `archive_items`, plus
    `organization_id` — plan ruling 6, not in the original design note, added
    so `GET /archive` can be zone-filtered without joining back through a
    polymorphic `object_type`/`object_id` pair into two different tables).

    `object_id` carries no FK: it points at `applications.id` OR `permits.id`
    depending on `object_type`, and a single column cannot reference two
    tables. `uq_archive_items_object` is what stops the same object being
    archived twice, standing in for the FK's usual "one row, one truth" role.

    `content_hash`/`storage_ref` are sha256 and the object-storage key of a
    canonical JSON snapshot written at archival time (plan ruling 5) — not a
    hash of the live row, so a later re-verification
    (`POST /archive/{id}/verify`) catches storage corruption, not database
    drift. `status` starts `stored` and becomes `verified` once that check
    has passed at least once; it never reverts on a passing re-check, only a
    FAILING one raises `ERR-ARCH-002` and leaves status untouched (the failure
    itself is the alarm — a service should not quietly downgrade a `verified`
    row back to `stored` on a check that passed the day before).
    """

    __tablename__ = "archive_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    object_type: Mapped[str]
    object_id: Mapped[uuid.UUID] = mapped_column(index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    archived_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retention_until: Mapped[date | None]
    content_hash: Mapped[str]
    storage_ref: Mapped[str]
    status: Mapped[str] = mapped_column(default="stored")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)

    __table_args__ = (
        CheckConstraint(f"object_type IN {ARCHIVABLE_OBJECT_TYPES}", name="object_type_valid"),
        CheckConstraint(f"status IN {ARCHIVE_ITEM_STATUSES}", name="status_valid"),
        UniqueConstraint("object_type", "object_id", name="uq_archive_items_object"),
    )
