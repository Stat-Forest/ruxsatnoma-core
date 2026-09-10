"""Applications — the entity every other module hangs off (design/02 § applications,
corrected by plan `03.9a-applications-core`).

Two invariants live in migration 0015 rather than here, because SQLAlchemy's metadata
cannot express them and Alembic does not diff them: the EXCLUDE constraint forbidding
two active applications for the same (applicant, contour, activity) on an overlapping
period (`ex_applications_no_duplicate`, ruling 6), and the append-only trigger on
`application_status_history` (mirroring `audit_log`'s, migration 0002, and
`calculations`'s, migration 0011). Both are documented on their class below.

Every enum-ish column has exactly one source of truth — the module-level tuples below,
each turned into a `CheckConstraint` — so a later branch-2 task can build its pydantic
`Literal`s from them by hand; no schemas live in this branch."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

# tz/05's full state machine, all 14 statuses from day one (ruling 2) even though
# 3.9a only ever writes DRAFT/SUBMITTED/IN_REVIEW/APPROVED/REJECTED/CANCELLED — the
# rest belong to 3.9b/3.10/3.11/archive (see the plan's ruling 2 for who writes each).
APPLICATION_STATUSES = (
    "DRAFT",
    "SUBMITTED",
    "IN_REVIEW",
    "PENDING_INFO",
    "RETURNED",
    "APPROVED",
    "INVOICED",
    "PAID",
    "PERMIT_ISSUED",
    "REJECTED",
    "CANCELLED",
    "EXPIRED_UNPAID",
    "CLOSED",
    "ARCHIVED",
)
ON_BEHALF_VALUES = ("self", "legal")
CHANNELS = ("portal", "mygov")
APPLICATION_KINDS = ("new", "extension")
# Ruling #179 (decisions.md): a SEPARATE state machine from `status` above,
# living on the same row on purpose — the benefit claim and the application
# itself are decided by different people at different moments, and a claim
# rejection must never be confused with `status="REJECTED"` (the head's own
# refusal of the whole filing). `not_required` is the default for every
# application, benefit or none: only a row that actually CLAIMS a category
# (`benefit_category_item_id` set) ever leaves it — ruling #181 made the
# certificate number mandatory for every category, so `requires_certificate`
# is no longer read anywhere; a claimed category with no number is refused at
# submission rather than filed as `not_required`. `pending` -> `verified` or
# `rejected` is either the seam `service._open_benefit_verification` calls at
# submission (ruling #182's registered auto-verifiers, `verified` on the
# spot) or the leshoz's own `benefit_verification.py` (`verify_claim`/
# `.reject_claim`, `benefits.verify`, in zone).
BENEFIT_VERIFICATION_STATUSES = ("not_required", "pending", "verified", "rejected")

# ruling 21: what gis.checks and norms.checks actually emit. gis's own `restrictions`
# is dropped (a strictly weaker duplicate of norm_restrictions over the same three
# layers); vet/cadastre are 3.9b's, kept here for the same reason as the unused
# statuses above — a known future writer.
CHECK_TYPES = (
    "gis_validity",
    "gis_within_fund",
    "gis_overlap",
    "norm_available",
    "norm_season",
    "norm_rotation",
    "norm_fire_ban",
    "norm_restrictions",
    "norm_limit",
    "vet",
    "cadastre",
)
# The same audit ruling 21 performed for `check_type`, carried to `result` (final
# review C1): read off every literal `gis.checks` and `norms.checks` can return, not
# off design/02's prediction, which stops at three. `skipped` is the fourth and it is
# the COMMON path, not an edge case — `gis.checks._within_fund` returns it for every
# contour while the Agency's `forest_fund` layer is empty (a designed branch, not a
# temporary state), and `norms.checks` returns it for a non-grazing activity, a norm
# with no season window, a contour with no published geometry, and an uncomputed
# limit. Branch 2 writes back whatever the two modules said (ruling 12: the row is
# evidence), so a CHECK three values wide is an IntegrityError on the first
# submission the system ever attempts.
CHECK_RESULTS = ("pass", "fail", "warning", "skipped")
CHECK_SOURCES = ("auto", "external_api", "manual_fallback")
ASSIGNMENT_REASONS = ("auto", "absence", "manual")
CONCLUSION_KINDS = ("executor", "gis")
CONCLUSION_RECOMMENDATIONS = ("approve", "reject")


class Application(Base):
    """The application (design/02 § applications). `activity_type_id`, `contour_id`,
    `contour_version_id`, `period_from`/`period_to` and `requested_area_ha` are all
    nullable (ruling 7): a DRAFT is autosaved field by field (tz/04 С3) and must be
    storable half-empty. Full validation runs later, in precheck and submit — not
    here. `requested_area_ha` is frozen at submission from the contour version's own
    `area_ha` (ruling 22); it is not a caller-settable field.

    Migration 0015 adds, and this class cannot express:
      EXCLUDE USING gist (applicant_id WITH =, contour_id WITH =, activity_type_id WITH =,
                          daterange(period_from, period_to, '[]') WITH &&)
      WHERE (status IN ('SUBMITTED','IN_REVIEW','PENDING_INFO','RETURNED','APPROVED',
                        'INVOICED','PAID','PERMIT_ISSUED')
             AND contour_id IS NOT NULL AND period_from IS NOT NULL AND period_to IS NOT NULL)
    named `ex_applications_no_duplicate` (ruling 6) — the service matches on that name.
    DRAFT sits outside the WHERE clause on purpose: a duplicate is caught at
    submission, not while the applicant is still typing.
    """

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str | None] = mapped_column(unique=True)
    applicant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applicants.id"), index=True)
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    on_behalf: Mapped[str]
    representation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("representations.id"), index=True
    )
    activity_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("activity_types.id"), index=True
    )
    contour_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contours.id"), index=True)
    contour_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contour_versions.id"), index=True
    )
    requested_area_ha: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    period_from: Mapped[date | None]
    period_to: Mapped[date | None]
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    status: Mapped[str] = mapped_column(default="DRAFT")
    channel: Mapped[str]
    mygov_reference: Mapped[str | None] = mapped_column(unique=True)
    sla_deadline_at: Mapped[datetime | None]
    assigned_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    parent_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    kind: Mapped[str] = mapped_column(default="new")
    rejection_reason_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    benefit_category_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    # Ruling #179's five columns. `benefit_certificate_no` is applicant input
    # (added to `ApplicationPatch`, stored exactly like every other draft
    # field — no format is prescribed, the Agency's registries vary by
    # category); the remaining four are never client-settable and are written
    # only by `applications.service.submit` (the `pending`/`not_required`
    # split, per this file's `BENEFIT_VERIFICATION_STATUSES` docstring) and by
    # `benefit_verification.py`'s verify/reject (`verified`/`rejected`, plus
    # who and when — `benefit_rejection_reason` is NULL for every status but
    # `rejected`, where it is mandatory).
    benefit_certificate_no: Mapped[str | None]
    benefit_verification_status: Mapped[str] = mapped_column(default="not_required")
    benefit_verified_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True
    )
    benefit_verified_at: Mapped[datetime | None]
    benefit_rejection_reason: Mapped[str | None]
    decision_basis: Mapped[str | None]
    submitted_at: Mapped[datetime | None]
    decided_at: Mapped[datetime | None]
    # Ruling #184: "I have read the rules" is a mandatory acceptance before any
    # signature. NULL for every DRAFT/RETURNED row (nothing accepted yet) and
    # for a row submitted before this column existed — a historical submission
    # is not retroactively un-accepted. Written server-side, once, by
    # `service.submit` alone; never a client-settable field.
    rules_accepted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(f"status IN {APPLICATION_STATUSES}", name="status_valid"),
        CheckConstraint(f"on_behalf IN {ON_BEHALF_VALUES}", name="on_behalf_valid"),
        CheckConstraint(f"channel IN {CHANNELS}", name="channel_valid"),
        CheckConstraint(f"kind IN {APPLICATION_KINDS}", name="kind_valid"),
        CheckConstraint(
            f"benefit_verification_status IN {BENEFIT_VERIFICATION_STATUSES}",
            name="benefit_verification_status_valid",
        ),
        # Every row carrying an active benefit claim — a small fraction of the
        # table. Ruling #179 built this for the (now-retired, ruling #182) central
        # office's own country-wide query; kept as a general-purpose filter for
        # any future reader of "which applications carry a claim". A manually
        # named partial index, declared here (not just in the migration) so the
        # autogenerate-diff guard stays empty — `ApplicationAssignment.
        # __table_args__`'s `uq_application_assignments_active` below is the
        # identical shape, one class over.
        Index(
            "ix_applications_benefit_verification_pending",
            "benefit_verification_status",
            postgresql_where=text("benefit_verification_status <> 'not_required'"),
        ),
        # `repo.list_applications` orders by `updated_at DESC, id DESC` (a
        # backward scan of this ascending index), so a country-wide page for a
        # republic-scoped reader is an index walk with an early stop rather than
        # a full sort of the table. Declared here as well as in migration 0057
        # so the autogenerate-diff guard stays empty.
        Index("ix_applications_updated_at_id", "updated_at", "id"),
    )


class ApplicationItem(Base):
    """Livestock by kind and age group (design/02 § application_items)."""

    __tablename__ = "application_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # No index=True here (M1): it would duplicate the leading column of the
    # unique index behind uq_application_items_livestock below.
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"))
    livestock_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("livestock_types.id"), index=True
    )
    head_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("head_count > 0", name="head_count_positive"),
        UniqueConstraint(
            "application_id", "livestock_type_id", name="uq_application_items_livestock"
        ),
    )


class ApplicationDocument(Base):
    """A document attached to an application (design/02 § application_documents)."""

    __tablename__ = "application_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    doc_type_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"), index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    note: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApplicationStatusHistory(Base):
    """The timeline (design/02 § application_status_history). Append-only via
    migration 0015's BEFORE UPDATE/DELETE/TRUNCATE trigger, mirroring `audit_log`'s
    (0002) and `calculations`'s (0011) own — autogenerate cannot see either, so the
    trigger and its function live only in the migration.

    `id` keeps its `uuid7` default here, but `submit` supplies it explicitly for the
    SUBMITTED row: it is the submission id the signature is bound to (ruling 25). No
    column or constraint change follows from that — it is simply a value a later
    task's service passes in instead of leaving to the default.

    **Order the timeline by `(occurred_at, id)`, never `occurred_at` alone** (final
    review M3). `occurred_at` is `now()`, which in Postgres is TRANSACTION start
    time, so every row written in one transaction shares it to the microsecond —
    and that is the normal case, not a rarity: branch 2's `approve()` writes the
    APPROVED row and publishes `application_approved`, whose 3.10a handler runs in
    the SAME transaction (ruling 3а) and writes INVOICED beside it. `id` is `uuid7`
    and therefore time-ordered, so it agrees with insertion order and breaks the tie
    correctly; `ix_application_status_history_timeline` carries all three columns so
    the ordering is served by the index. `norms.repo._calculations_query` documents
    the identical tie-break for `calculations`.
    """

    __tablename__ = "application_status_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    # No index=True here (M1): it would be a strict prefix of
    # ix_application_status_history_timeline (application_id, occurred_at, id) below.
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"))
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    reason_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    reason_text: Mapped[str | None]
    legal_basis: Mapped[str | None]
    fields_to_fix: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"to_status IN {APPLICATION_STATUSES}", name="to_status_valid"),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN {APPLICATION_STATUSES}",
            name="from_status_valid",
        ),
        Index("ix_application_status_history_timeline", "application_id", "occurred_at", "id"),
    )


class ApplicationAssignment(Base):
    """The assignment history (design/02 § application_assignments, C4). At most one
    active assignment per application — the partial unique index below."""

    __tablename__ = "application_assignments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str]
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"reason IN {ASSIGNMENT_REASONS}", name="reason_valid"),
        Index(
            "uq_application_assignments_active",
            "application_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class InfoRequest(Base):
    """A request for additional information — PENDING_INFO, SLA paused (design/02 §
    info_requests). The SLA pause is the sum of the `requested_at` -> `responded_at`
    intervals, computed in code, not here."""

    __tablename__ = "info_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    message: Mapped[str]
    requested_at: Mapped[datetime] = mapped_column(server_default=func.now())
    responded_at: Mapped[datetime | None]
    response_text: Mapped[str | None]


class ApplicationConclusion(Base):
    """A conclusion on an application (design/02 § application_conclusions, C5/C8:
    "the GIS conclusion, the executor's finding"). A repeat conclusion after rework
    is a new row, never an update."""

    __tablename__ = "application_conclusions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str]
    text: Mapped[str]
    recommendation: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(f"kind IN {CONCLUSION_KINDS}", name="kind_valid"),
        CheckConstraint(
            f"recommendation IS NULL OR recommendation IN {CONCLUSION_RECOMMENDATIONS}",
            name="recommendation_valid",
        ),
    )


class ApplicationCheck(Base):
    """Results of automatic and external checks (design/02 § application_checks,
    corrected by ruling 21 — the eleven `check_type` values below, replacing
    design/02's `gis_restrictions` with `norm_restrictions`). A repeat check is a new
    row; the history is preserved (ruling 12) — never an update.

    `result` carries all four values the two check modules emit — `pass`, `fail`,
    `warning`, `skipped` — see `CHECK_RESULTS` above for why `skipped` is not
    optional.

    3.9a always writes `source='auto'` and leaves `doc_file_id` null; both columns
    exist from day one because 3.9b's manual fallback needs them.

    `created_by`/`confirmed_by`/`confirmed_at` are migration `0025`'s three
    columns for task 7's maker-checker (ruling 15, ANSWERED а) — every row
    names WHO created it (3.9a's own auto checks attribute it to the
    application's `submitted_by_user_id`, `checks.run_all`'s own reasoning),
    while only a MANUAL check that has actually been confirmed carries the
    other two."""

    __tablename__ = "application_checks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id"), index=True)
    check_type: Mapped[str]
    result: Mapped[str]
    details: Mapped[Any] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(default="auto")
    doc_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"), index=True)
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(f"check_type IN {CHECK_TYPES}", name="check_type_valid"),
        CheckConstraint(f"result IN {CHECK_RESULTS}", name="result_valid"),
        CheckConstraint(f"source IN {CHECK_SOURCES}", name="source_valid"),
    )
