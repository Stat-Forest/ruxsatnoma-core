"""Permits — the document the whole system exists to produce (design/02 § permits,
corrected by plan `03.11a-permits-core`).

All seven tables land in this one migration, including the three whose writers are
3.11b (`permit_duplicates`, `forest_tickets`) and Task 5 (`qr_check_log`): a table
costs nothing to create, and a migration in a block shared with two other sessions
costs a coordination round.

Two things live in migration 0019 rather than here, because SQLAlchemy's metadata
cannot express them and Alembic does not diff them: the append-only trigger on
`permit_status_history` (mirroring `audit_log`'s in 0002, `calculations`'s in 0011
and `application_status_history`'s in 0015), and the seeded rows — the `'А'` counter,
the default grazing template and this module's notification templates.

Every enum-ish column has exactly one source of truth: the module-level tuples below,
each turned into a `CheckConstraint`. A later task builds its pydantic `Literal`s from
them by hand and closes the gap with an equality assertion (lesson); no schemas live
in this task."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# tz/05's permit state machine, all six statuses from day one (Task 1 brief, the same
# reasoning as 3.9a ruling 2) even though 3.11a writes only `pending_signatures`,
# `active` and `expired`: `suspended`/`revoked` are 3.11b's (С13) and `archived` is
# 4.7's, and none of them may cost a migration to widen a constraint.
PERMIT_STATUSES = (
    "pending_signatures",
    "active",
    "suspended",
    "revoked",
    "expired",
    "archived",
)
# A template row an administrator prepares before it takes effect, then supersedes by
# version — the same three-step life `norms`/`tariffs` have (draft -> published ->
# archived) and the reason `draft` is here although 0019 seeds an `active` row.
TEMPLATE_STATUSES = ("draft", "active", "archived")
FOREST_TICKET_STATUSES = ("active", "expired", "revoked")
QR_CHECK_RESULTS = ("found", "not_found")
QR_CHECK_CHANNELS = ("qr", "manual")


class Permit(Base):
    """The permit on form 1-ilova (design/02 § permits, plus `doc_hash` — ruling 3).

    `snapshot` is immutable (`tz/05` invariant 7): every field the PDF shows is copied
    here at issuance and read from here forever after, so a citizen renaming themselves
    tomorrow does not change a permit issued today.

    `doc_hash` is the sha256 of the stored PDF bytes and is a column `design/02` does
    not have (ruling 3). It is null until the document is rendered, set once, and never
    rewritten: every one of the 3+1 signatures is taken over exactly those bytes, which
    is what makes `signatures.service.require_complete` mean what it says.

    **Nothing re-renders a permit, because 3.11a exposes no entry point that could**:
    `service.issue` renders once and `GET /permits/{id}/pdf` serves the stored file.
    That is stronger than a guard, and it is why `ERR-PERM-002` ("документ уже
    подписан и не может быть перевыпущен") is registered in `app/core/errors.py` and
    raised by nothing. It is reserved, not dead: 3.11b's duplicate register (нусха)
    must copy these same bytes rather than re-render them, and the day any path can
    reach the renderer with an already-signed permit, that path raises this code.

    `status` is never set from anywhere but the one function that asks
    `signatures.service` whether the required set is complete — `active` means every
    required signature is valid (`design/02`: "ACTIVE only after all 3+1 signatures").

    Nullable on purpose, each with a named writer: `pdf_file_id`/`doc_hash`/`template_id`
    are filled by issuance once the document exists; `issued_at` by the transition to
    `active` (ruling 18 — `tz/05` defines the issued state as «сформировано **и
    подписано**»); `sb_load` stays null for an activity that commits no conditional-head
    load at all (haymaking, apiaries), where a stored `0` would read as "no animals"
    rather than "not applicable" and would be summed as a fact by
    `norms.service.LOAD_PROVIDERS`.
    """

    __tablename__ = "permits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # No index=True on `series`: `uq_permits_series_number` below already leads with it.
    series: Mapped[str]
    number: Mapped[int] = mapped_column(BigInteger)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), unique=True)
    applicant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applicants.id"), index=True)
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    contour_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contours.id"), index=True)
    contour_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contour_versions.id"), index=True
    )
    area_ha: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    period_from: Mapped[date]
    period_to: Mapped[date]
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    sb_load: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str] = mapped_column(default="pending_signatures")
    pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    doc_hash: Mapped[str | None]
    qr_token: Mapped[str] = mapped_column(unique=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("permit_templates.id"), index=True
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    issued_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # tz/05 invariant 2 — the database is the guarantee, not the service.
        UniqueConstraint("series", "number"),
        CheckConstraint(f"status IN {PERMIT_STATUSES}", name="status_valid"),
        CheckConstraint("number > 0", name="number_positive"),
        # A reversed period inverts every range predicate written over these two
        # columns and hides the rows it should find (lesson) — and ruling 11's
        # LoadProvider is exactly such a predicate, over exactly this pair.
        CheckConstraint("period_to >= period_from", name="period_ordered"),
    )


class PermitCounter(Base):
    """Race-free series numbering (design/02 § permit_counters, ruling 9).

    Issuing is one statement inside the issuing transaction —
    `UPDATE permit_counters SET last_number = last_number + 1 WHERE series = :s
    RETURNING last_number` — never SELECT-then-UPDATE: the row lock is held for the
    length of that transaction, so a number cannot be handed out twice.

    The permit is the one object OUTSIDE the `{PREFIX}-{YEAR}-{NUMBER}` scheme of
    `core.numbers` (`design/03` § Public numbers), which is why this counter exists
    beside `number_counters` instead of reusing it. Migration 0019 seeds the single
    row for series `'А'`; a second series is a second row, not a migration."""

    __tablename__ = "permit_counters"

    series: Mapped[str] = mapped_column(primary_key=True)
    last_number: Mapped[int] = mapped_column(BigInteger, server_default="0")


class PermitStatusHistory(Base):
    """The permit's timeline (design/02 § permit_status_history). Append-only via
    migration 0019's BEFORE UPDATE/DELETE/TRUNCATE trigger, mirroring `audit_log`'s
    (0002), `calculations`'s (0011) and `application_status_history`'s (0015) —
    autogenerate cannot see any of them, so the trigger and its function live only in
    the migration. A correction is a new row.

    **Order the timeline by `(occurred_at, id)`, never `occurred_at` alone** — the same
    tie-break `application_status_history` documents: `occurred_at` is `now()`, which in
    PostgreSQL is TRANSACTION start time, so every row written in one transaction shares
    it to the microsecond. `id` is `uuid7` and therefore time-ordered, and
    `ix_permit_status_history_timeline` carries all three columns so the ordering is
    served by the index.

    EXPIRED is written by the daily job (design/02), not by a request."""

    __tablename__ = "permit_status_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # No index=True here: it would be a strict prefix of
    # ix_permit_status_history_timeline (permit_id, occurred_at, id) below.
    permit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permits.id"))
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    reason_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    legal_basis: Mapped[str | None]
    doc_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"to_status IN {PERMIT_STATUSES}", name="to_status_valid"),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN {PERMIT_STATUSES}", name="from_status_valid"
        ),
        Index("ix_permit_status_history_timeline", "permit_id", "occurred_at", "id"),
    )


class PermitTemplate(Base):
    """The versioned layout a permit is rendered from (design/02 § permit_templates).

    `template_id` on the permit points at the exact row used at issuance, so a permit
    re-rendered from its own snapshot years later is byte-identical (ruling 14). A new
    layout is a new version, never an in-place rewrite of an issued permit's row.

    `layout_file_id` is nullable and the seeded grazing row leaves it null: a migration
    cannot put bytes in MinIO, and a row pointing at a storage key that does not exist
    would be worse than an honest null. Null means "the layout bundled with the module"
    (`app/modules/permits/assets/`, Task 2); an administrator uploading a layout fills
    the column and that row wins from then on."""

    __tablename__ = "permit_templates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # No index=True here: uq_permit_templates_activity_type_id_version leads with it.
    activity_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activity_types.id"))
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    layout_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_files.id"), index=True
    )
    status: Mapped[str] = mapped_column(default="active")
    valid_from: Mapped[date]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("activity_type_id", "version"),
        CheckConstraint(f"status IN {TEMPLATE_STATUSES}", name="status_valid"),
        CheckConstraint("version > 0", name="version_positive"),
        # Exactly one active version per activity type — the same partial unique index
        # every sibling versioned catalogue here carries (`notification_templates`
        # 0009, the admin and applicant catalogues, `contour_versions … WHERE
        # status='published'`). Without it the supersede lifecycle this docstring
        # promises is only a convention: two active grazing rows make "the active
        # template for this activity" return whichever row the plan order happens to
        # hand back, and issuance freezes the wrong `permits.template_id` forever
        # (review round 1, Important finding).
        #
        # A partial index constrains only the rows it covers, and only after a flush
        # (lesson): a supersede is `old.status = "archived"` -> `await db.flush()` ->
        # `db.add(new_row)`, in that order, and archived rows sit outside the index
        # so any number of them may coexist.
        Index(
            "uq_permit_templates_active",
            "activity_type_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class PermitDuplicate(Base):
    """The duplicate (нусха) register — design/02 § permit_duplicates, written by
    3.11b. A duplicate is a copy of the SAME bytes for a lost or damaged paper copy,
    never a re-render (ruling 3), which is why `file_id` is required."""

    __tablename__ = "permit_duplicates"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    permit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permits.id"), index=True)
    reason: Mapped[str]
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"), index=True)
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ForestTicket(Base):
    """The forest ticket, oʻrmon chiptasi (ВМҚ 506) — design/02 § forest_tickets,
    written by 3.11b. Its own number is a `core.numbers` public number (ChT), unlike
    the permit's series+number."""

    __tablename__ = "forest_tickets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str] = mapped_column(unique=True)
    permit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("permits.id"), index=True)
    valid_from: Mapped[date]
    valid_to: Mapped[date]
    restrictions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="active")
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {FOREST_TICKET_STATUSES}", name="status_valid"),
        CheckConstraint("valid_to >= valid_from", name="period_ordered"),
        # `ERR-PERM-003` (plan `03.11b-permits-lifecycle` ruling 19): a second
        # active ticket on one permit is a conflict, seeded by migration 0023.
        Index(
            "uq_forest_tickets_active",
            "permit_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class QrCheckLog(Base):
    """Anonymous verification statistics for the public QR page (design/02 §
    qr_check_log, written by Task 5).

    **No IP address and no personal data**, by design/02's explicit instruction: the
    page it counts is unauthenticated, so anything identifying stored beside a permit
    id would be a record of who looked at whose permit. `permit_id` is null when the
    lookup found nothing — that null IS the `not_found` case's whole payload.

    BRIN on `occurred_at` (an append-only, time-ordered log — the same index shape
    `audit_log` and `integration_log` carry)."""

    __tablename__ = "qr_check_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    permit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("permits.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    result: Mapped[str]
    channel: Mapped[str]

    __table_args__ = (
        CheckConstraint(f"result IN {QR_CHECK_RESULTS}", name="result_valid"),
        CheckConstraint(f"channel IN {QR_CHECK_CHANNELS}", name="channel_valid"),
        Index("ix_qr_check_log_occurred_at_brin", "occurred_at", postgresql_using="brin"),
    )
