"""Reports — forms 2-ilova / 3-ilova and the Яратилган -> Тасдиқланган workflow
(design/02 § reports, plan `04.3-reports` task 1).

Two tables, both already shaped by design/02 before this stage: `report_forms`
(the form builder — a versioned catalog row central_admin edits) and `reports`
(one submitted report per form/organization/period/version).

**R1 (plan ruling 1): an APPROVED report is frozen at the database level.**
`reports_forbid_mutation` (a `BEFORE UPDATE` trigger, added in the migration —
not expressible as a SQLAlchemy construct) raises whenever `OLD.status =
'approved'`, the same "this row now IS the record" shape `audit_log` and
`calculations` already use elsewhere in this codebase. A correction after
approval is a NEW row (`version_no + 1`, `parent_report_id` set), never an
edit in place — see `service.revise_report`.

Level 5 reader (design/01 rule 5): this module's OWN tables are `report_forms`
and `reports`; `repo.py` additionally SELECTs (never writes) `permits`,
`invoices` and `applicants` to build a report's `data.rows` — the reader
exception, not a cross-module service call, because the point is one join
across tables nobody's `service` frames as a single question."""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, uuid7

PERIOD_TYPES = ("month", "quarter", "year")
FORM_STATUSES = ("draft", "active", "archived")
# tz/04 С20 workflow, verbatim: hodim fills (created) -> sent to the rahbar
# (submitted) -> the rahbar signs ERI (head_approved) -> the central office
# accepts (approved) or either the rahbar or the central office sends it back
# (returned, `returned_by` says which). Plan ruling 3: a return re-enters the
# SAME row rather than forking a new one — see `Report.returned_by`.
REPORT_STATUSES = ("created", "submitted", "head_approved", "returned", "approved")
RETURNED_BY = ("head", "center")


class ReportForm(Base):
    """The form builder (2-ilova / 3-ilova and future ones), design/02 § reports.

    `columns`/`rules`/`schedule` are jsonb by design — С20: "Центр создаёт форму
    отчёта (название, тип, период, колонки, правила)" is an admin-editable
    catalog, not a compile-time shape. `rules` is DESCRIPTIVE here (what a
    control ratio IS, for display); `app/modules/reports/rules.py` is where the
    two seeded forms' ratios are actually enforced at `submit` (plan's "scope
    cuts" — no rule-DSL interpreter until a real one is specified).

    `(code, version)` unique: a new version of "2-ilova" is a new row, the same
    versioned-catalog shape `permit_templates`/`rule_parameters` already use —
    an in-force `reports` row keeps pointing at the exact `form_id` it was
    filled against even after a newer version goes `active`.
    """

    __tablename__ = "report_forms"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    code: Mapped[str]
    version: Mapped[int]
    name: Mapped[dict[str, Any]] = mapped_column(JSONB)
    activity_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("activity_types.id"))
    period_type: Mapped[str]
    columns: Mapped[list[Any]] = mapped_column(JSONB)
    rules: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(default="draft")
    valid_from: Mapped[date | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_report_forms_code_version"),
        CheckConstraint(f"period_type IN {PERIOD_TYPES}", name="period_type_valid"),
        CheckConstraint(f"status IN {FORM_STATUSES}", name="status_valid"),
    )


class Report(Base):
    """One submitted report: a form, an organization, a period, a version.

    `data` holds the rows `service.generate_report` computes (one row per
    matching permit, per `repo.report_rows`) plus whatever a hodim edited by
    hand afterwards — `{"rows": [...], "generated_at": "...iso..."}`. Mutable
    while `status` is `created`/`returned` (the hodim is still working on it);
    frozen forever from `approved` onward (see the module docstring's R1).

    `parent_report_id` chains a POST-APPROVAL correction to the row it
    corrects (plan ruling 1) — never set for an ordinary return-and-resubmit,
    which reuses `returned_by` on the SAME row instead (plan ruling 3).
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    form_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("report_forms.id"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    period_start: Mapped[date]
    period_end: Mapped[date]
    version_no: Mapped[int] = mapped_column(default=1)
    parent_report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reports.id"))
    status: Mapped[str] = mapped_column(default="created")
    returned_by: Mapped[str | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filled_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None]
    returned_comment: Mapped[str | None]
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    due_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "form_id",
            "organization_id",
            "period_start",
            "version_no",
            name="uq_reports_form_org_period_version",
        ),
        CheckConstraint(f"status IN {REPORT_STATUSES}", name="status_valid"),
        CheckConstraint(
            f"returned_by IS NULL OR returned_by IN {RETURNED_BY}", name="returned_by_valid"
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        # A hodim's own worklist reads "my organization's reports by status" —
        # the plain FK index on `organization_id` alone does not serve that
        # predicate.
        Index("ix_reports_organization_status", "organization_id", "status"),
    )
