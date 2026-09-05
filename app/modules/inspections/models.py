"""Field inspections (design/02 § inspections, plan `04.1-inspections`): checklists,
assignments (`inspection_tasks`), field acts (`inspection_acts` + their photo/video
attachments), and violation cases with their appeals and history.

Level 5 (design/01: "add-ons and channels") — this module reaches `applications`,
`permits` and `gis` only through their own `service`, never their `repo`/`models`.

`organization_id` on `inspection_tasks`/`inspection_acts`/`violation_cases` is an
addition beyond design/02's own text (plan ruling, "Schema" section): every other
zone-scoped table in this codebase (`applications.assigned_org_id`,
`permits.organization_id`) stores its own zone directly rather than re-deriving it
on every read via a join, and it is resolved once, at creation, from whichever of
task/permit/application/contour is present.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

TASK_KINDS = ("pre_approval_visit", "permit_inspection")
TASK_STATUSES = ("assigned", "in_progress", "done", "cancelled")
ACT_RESULTS = ("compliant", "warning", "violation")
ACT_STATUSES = ("draft", "signed")
ACT_FILE_KINDS = ("photo", "video")
CASE_STATUSES = (
    "opened",
    "explanation_requested",
    "explained",
    "decided",
    "appealed",
    "closed",
    "archived",
)
CASE_DECISIONS = ("warning", "suspend", "revoke", "transfer")


class Checklist(Base):
    """A versioned checklist template (design/02: "the checklist builder"). One
    ACTIVE row per `code` (partial unique below) — a new version supersedes by
    archiving the old row and inserting a new one, the same idiom
    `admin.service.supersede_classifier_item` uses for classifier items, kept
    separate from that table because a checklist's `items` shape (questions,
    answer types, whether mandatory) is domain-specific to this module, not a
    generic classifier value."""

    __tablename__ = "checklists"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    activity_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("activity_types.id"), index=True
    )
    items: Mapped[list[Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="active")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'archived')", name="status_valid"),
        CheckConstraint("version > 0", name="version_positive"),
        UniqueConstraint("code", "version", name="uq_checklists_code_version"),
        Index(
            "uq_checklists_active_code",
            "code",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class InspectionTask(Base):
    """An assignment — the site visit of C6, or a field inspection of C15.
    At least one of `application_id`/`permit_id`/`contour_id` must be set
    (CHECK below); `organization_id` is resolved from whichever one is."""

    __tablename__ = "inspection_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str]
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    permit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("permits.id"), index=True)
    contour_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contours.id"), index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    assigned_to: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    due_at: Mapped[date]
    status: Mapped[str] = mapped_column(default="assigned")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('pre_approval_visit', 'permit_inspection')", name="kind_valid"),
        CheckConstraint(
            "status IN ('assigned', 'in_progress', 'done', 'cancelled')", name="status_valid"
        ),
        CheckConstraint(
            "application_id IS NOT NULL OR permit_id IS NOT NULL OR contour_id IS NOT NULL",
            name="has_a_subject",
        ),
    )


class InspectionAct(Base):
    """The field act. All three of `task_id`/`permit_id`/`application_id` null
    plus a `gps` fix is an "activity without a permit" act (design/02).
    `checklist_id` names the EXACT version answered, never "the current one"
    — a later checklist edit must not reinterpret a past act's `answers`.
    The signature lives in `signatures` (`object_type="inspection_act"`,
    `purpose="act_sign"`) — see ruling 1 of the plan."""

    __tablename__ = "inspection_acts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inspection_tasks.id"), index=True)
    permit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("permits.id"), index=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id"), index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    inspector_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    occurred_at: Mapped[datetime]
    gps: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326))
    gps_accuracy_m: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    distance_to_contour_m: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    checklist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("checklists.id"), index=True)
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[str | None]
    notes: Mapped[str | None]
    created_offline_at: Mapped[datetime | None]
    synced_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(default="draft")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "result IS NULL OR result IN ('compliant', 'warning', 'violation')",
            name="result_valid",
        ),
        CheckConstraint("status IN ('draft', 'signed')", name="status_valid"),
    )


class InspectionActFile(Base):
    """One photo/video attached to an act. Metadata (GPS, time, hash) lives on
    `media_files` itself (design/02) — this row is the link plus what KIND of
    attachment it is."""

    __tablename__ = "inspection_act_files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    act_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inspection_acts.id"), index=True)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("media_files.id"), index=True)
    kind: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('photo', 'video')", name="kind_valid"),
        UniqueConstraint("act_id", "file_id", name="uq_inspection_act_files_act_file"),
    )


class ViolationCase(Base):
    """A violation case (design/02). Opened automatically when a signed act's
    `result='violation'` (in code, `service.sign_act`) — `act_id` is therefore
    NOT NULL: every case traces to the act that raised it."""

    __tablename__ = "violation_cases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    number: Mapped[str] = mapped_column(unique=True)
    act_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("inspection_acts.id"), index=True)
    permit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("permits.id"), index=True)
    applicant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("applicants.id"), index=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    violation_type_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("classifier_items.id"), index=True
    )
    status: Mapped[str] = mapped_column(default="opened")
    explanation_due_at: Mapped[date | None]
    explanation_text: Mapped[str | None]
    explanation_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("media_files.id"))
    damage_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    damage_calc: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decision: Mapped[str | None]
    decision_due_at: Mapped[date | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('opened', 'explanation_requested', 'explained', 'decided',"
            " 'appealed', 'closed', 'archived')",
            name="status_valid",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('warning', 'suspend', 'revoke', 'transfer')",
            name="decision_valid",
        ),
        CheckConstraint("damage_amount IS NULL OR damage_amount >= 0", name="damage_non_negative"),
    )


class ViolationAppeal(Base):
    """One appeal against a case's decision (design/02)."""

    __tablename__ = "violation_appeals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("violation_cases.id"), index=True)
    filed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str]
    filed_at: Mapped[datetime]
    result: Mapped[str | None]
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ViolationCaseHistory(Base):
    """The case's timeline (design/02). Append-only at the DB level — a
    row-level trigger raises on UPDATE/DELETE, a statement-level one on
    TRUNCATE, the exact shape `audit_log`/`calculations`/
    `application_status_history`/`permit_status_history` already use
    (migration `0026` writes both by hand; autogenerate cannot see them)."""

    __tablename__ = "violation_case_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("violation_cases.id"), index=True)
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    note: Mapped[str | None]

    __table_args__ = (Index("ix_violation_case_history_timeline", "case_id", "occurred_at", "id"),)
