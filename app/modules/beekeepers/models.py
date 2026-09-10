"""beekeepers module models (stage 10, rulings #181/#182): the Beekeeping
Union's own register of certificate holders.

This is the register `service.match_certificate` reads on every filing that
claims the `beekeeping_union_member` benefit — a pure lookup of THIS table,
no application knowledge. Rows are never deleted (design/02 principle 7, the
same posture every other register in this codebase takes): removing a member
sets `status='removed'` with a mandatory `removed_reason`, so a later
re-registration under the SAME certificate number is a NEW active row, never
a resurrection of the old one. `uq_beekeepers_certificate_no_active` is a
PARTIAL unique index — `status='active'` only — exactly the idiom
`admin.models.ClassifierItem`'s own `uq_classifier_items_active_code` already
uses for the identical reason: a removed row must never block a future
active one under the same number.

`pinfl`/`stir` are TEXT with a CHECK, not a literal SQL `CHAR(n)` — matching
CLAUDE.md's stated convention ("strings are TEXT, statuses/formats
constrained by CHECK, not varchar length") and every other identity column
in this schema (`auth.models.User.pinfl`, `admin.models.Organization.stir`),
not a new precedent for this one table.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

BEEKEEPER_STATUSES = ("active", "removed")


class Beekeeper(Base):
    """One certificate-holder row. `pinfl` is the individual member's identity;
    `stir` is filled when the member is a legal entity — `service.
    match_certificate`'s identity rule (ruling #182) picks whichever the
    caller supplies, PINFL first, but neither column here is exclusive of the
    other: a legal entity's certificate may still name the person who holds
    it in `pinfl`."""

    __tablename__ = "beekeepers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    certificate_no: Mapped[str]
    pinfl: Mapped[str]
    passport_series: Mapped[str]
    passport_number: Mapped[str]
    stir: Mapped[str | None]
    full_name: Mapped[str]
    farm_name: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="active")
    removed_reason: Mapped[str | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'removed')", name="status_valid"),
        CheckConstraint(
            "status <> 'removed' OR "
            "(removed_reason IS NOT NULL AND length(trim(removed_reason)) > 0)",
            name="removed_reason_required",
        ),
        CheckConstraint(r"pinfl ~ '^[0-9]{14}$'", name="pinfl_format"),
        CheckConstraint(r"stir IS NULL OR stir ~ '^[0-9]{9}$'", name="stir_format"),
        Index(
            "uq_beekeepers_certificate_no_active",
            "certificate_no",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_beekeepers_pinfl", "pinfl"),
    )
