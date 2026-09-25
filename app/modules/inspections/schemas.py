"""API shapes for inspections. `LocalizedName` (core/schemas) is used for every
piece of admin-authored text (checklist name, each question) — the same
uz_latn-required rule every other module's reference data follows (decision #90)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer

from app.core.schemas import BlobStr, CodeStr, JsonObject, LocalizedName, NoteStr, TextStr

# --- Request bounds (stage 17, QA run 01, task 8) -----------------------------
# `inspection_acts`/`violation_cases`' numeric columns are fixed-scale
# NUMERIC — `le=` mirrors that scale exactly (R4).
GPS_ACCURACY_M_MAX = Decimal("99999999.99")  # inspection_acts.gps_accuracy_m NUMERIC(10, 2)
DAMAGE_AMOUNT_MAX = Decimal("9999999999999999.99")  # violation_cases.damage_amount NUMERIC(18, 2)
CHECKLIST_ITEMS_MAX = 200  # R3
# `notes`/explanation mirror the adminka's own 4000-char textareas
# (`ActFormPage.tsx:312`, `CaseDetailPage.tsx:158`), wider than the stage-17
# default `TEXT_MAX_LENGTH`/2000 (Global Constraints: never bound below an
# existing adminka maxLength). Fix round 1 (QA run 01 review).
ACT_NOTES_MAX_LENGTH = 4000
EXPLANATION_TEXT_MAX_LENGTH = 4000
# Optional, blank-capable (like NoteStr, but wider).
ActNotesStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=0, max_length=ACT_NOTES_MAX_LENGTH)
]
# Required, non-blank (like TextStr, but wider).
ExplanationTextStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=EXPLANATION_TEXT_MAX_LENGTH),
]


def _trim_decimal(value: Decimal | None) -> str | None:
    """Fixed-scale NUMERIC columns round-trip through Postgres with trailing
    zeros (`Decimal('12.50')`) — this strips them before the API returns them,
    the same shape `gis.schemas._trim_decimal` uses for its own NUMERIC
    columns (a deliberately separate copy per module convention)."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class GpsPoint(BaseModel):
    """A GPS fix, `(lon, lat)` in WGS84 — never a raw string reaching a WKT
    literal (`core.files.set_capture_metadata`/`inspections.repo`'s own
    geometry writers build the literal from these bounded floats)."""

    lon: Annotated[float, Field(ge=-180, le=180)]
    lat: Annotated[float, Field(ge=-90, le=90)]


class ChecklistQuestion(BaseModel):
    code: CodeStr
    question: LocalizedName
    type: Literal["bool", "number", "text"]
    required: bool = False


class ChecklistIn(BaseModel):
    """`POST /inspections/checklists`: a NEW version. `code` existing already
    supersedes it (archive + insert, service-side) — never an in-place edit of
    a past act's own checklist."""

    code: CodeStr
    name: LocalizedName
    activity_type_id: uuid.UUID | None = None
    items: Annotated[list[ChecklistQuestion], Field(min_length=1, max_length=CHECKLIST_ITEMS_MAX)]


class ChecklistQuestionOut(BaseModel):
    """Same shape as `ChecklistQuestion`, but `code` is a plain, unbounded
    `str` (I5, final review): a STORED row may predate `CodeStr`'s 64-char
    bound, and a GET must still list it rather than 500 re-validating
    output through the same tightened type the input schema uses."""

    code: str
    question: LocalizedName
    type: Literal["bool", "number", "text"]
    required: bool = False


class ChecklistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    version: int
    name: LocalizedName
    activity_type_id: uuid.UUID | None
    items: list[ChecklistQuestionOut]
    status: str


class TaskIn(BaseModel):
    kind: Literal["pre_approval_visit", "permit_inspection"]
    application_id: uuid.UUID | None = None
    permit_id: uuid.UUID | None = None
    contour_id: uuid.UUID | None = None
    assigned_to: uuid.UUID
    due_at: date | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    application_id: uuid.UUID | None
    permit_id: uuid.UUID | None
    contour_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    assigned_to: uuid.UUID
    due_at: date
    status: str
    created_by: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime


class ReassignIn(BaseModel):
    """`POST /inspections/tasks/{id}/reassign` (ruling R6): the handover — the
    task keeps its id, its due date and its history, only `assigned_to`
    changes."""

    new_assignee_id: uuid.UUID


class ActCreateIn(BaseModel):
    """`POST /inspections/acts`: all three of `task_id`/`permit_id`/
    `application_id` left unset plus a `gps` fix is an "activity without a
    permit" act (design/02, tz/04 С15)."""

    task_id: uuid.UUID | None = None
    permit_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    occurred_at: datetime
    gps: GpsPoint | None = None
    gps_accuracy_m: Decimal | None = Field(default=None, ge=0, le=GPS_ACCURACY_M_MAX)
    checklist_id: uuid.UUID
    answers: JsonObject = Field(default_factory=dict)
    facts: JsonObject = Field(default_factory=dict)
    notes: ActNotesStr | None = None
    result: Literal["compliant", "warning", "violation"] | None = None
    created_offline_at: datetime | None = None

    @field_serializer("gps_accuracy_m")
    def _ser_accuracy(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class ActUpdateIn(BaseModel):
    """`PATCH /inspections/acts/{id}`: DRAFT-only, own act (`ERR-INSP-001`
    otherwise) — the same "one editable status" shape `applications`'
    `application_documents` used before 3.9b widened it."""

    occurred_at: datetime | None = None
    gps: GpsPoint | None = None
    gps_accuracy_m: Decimal | None = Field(default=None, ge=0, le=GPS_ACCURACY_M_MAX)
    answers: JsonObject | None = None
    facts: JsonObject | None = None
    notes: ActNotesStr | None = None
    result: Literal["compliant", "warning", "violation"] | None = None


class ActFileIn(BaseModel):
    """`POST /inspections/acts/{id}/files`: `file_id` names an already-uploaded
    row (generic `POST /files`, this module invents no storage of its own).
    `taken_at`/`gps`/`device` fill the SAME row's capture-metadata columns
    (`core.files.set_capture_metadata`) — unused since 3.3b until this stage."""

    file_id: uuid.UUID
    kind: Literal["photo", "video"]
    taken_at: datetime | None = None
    gps: GpsPoint | None = None
    device: JsonObject | None = None


class ActSignIn(BaseModel):
    """`POST /inspections/acts/{id}/sign` (ruling 1 of the plan). `pkcs7` is
    the E-IMZO envelope over the act's own canonical bytes
    (`service._act_package_bytes`). `violation_type_item_id` is REQUIRED when
    the act's own `result` is `"violation"` — neither the checklist nor the
    act names which of VT-01…06 applies, so the inspector classifies it here,
    at the moment of finalizing."""

    pkcs7: BlobStr
    violation_type_item_id: uuid.UUID | None = None


class ActFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    act_id: uuid.UUID
    file_id: uuid.UUID
    kind: str
    created_at: datetime


class ActOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID | None
    permit_id: uuid.UUID | None
    application_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    inspector_id: uuid.UUID
    occurred_at: datetime
    gps_accuracy_m: Decimal | None
    distance_to_contour_m: Decimal | None
    checklist_id: uuid.UUID
    answers: dict[str, Any]
    facts: dict[str, Any]
    result: str | None
    notes: str | None
    status: str
    created_offline_at: datetime | None
    synced_at: datetime | None
    created_at: datetime

    @field_serializer("gps_accuracy_m", "distance_to_contour_m")
    def _ser_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class ActCardOut(ActOut):
    """`GET /inspections/acts/{id}`: the act's own columns, FLAT, plus its
    attached files and the GPS fix as plain floats — the same "card" idiom
    `permits.PermitCardOut` uses for a single-object read richer than the
    list row (never nested: the card and one row of the list are the same
    object seen at two depths)."""

    gps: GpsPoint | None
    files: list[ActFileOut]

    @classmethod
    def build(cls, card: dict[str, Any]) -> ActCardOut:
        """Assemble from `service.act_card`'s dict — `ActOut.model_fields` is
        read rather than the column names retyped, the same reflection
        `permits.schemas.PermitCardOut.build` uses for itself."""
        act = card["act"]
        return cls.model_validate(
            {
                **{name: getattr(act, name) for name in ActOut.model_fields},
                "gps": card["gps"],
                "files": card["files"],
            }
        )


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    act_id: uuid.UUID
    permit_id: uuid.UUID | None
    applicant_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    violation_type_item_id: uuid.UUID
    status: str
    explanation_due_at: date | None
    explanation_text: str | None
    explanation_file_id: uuid.UUID | None
    damage_amount: Decimal | None
    decision: str | None
    decision_due_at: date | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime

    @field_serializer("damage_amount")
    def _ser_damage(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class CaseHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status: str | None
    to_status: str
    changed_by: uuid.UUID | None
    occurred_at: datetime
    note: str | None


class AppealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    filed_by: uuid.UUID
    text: str
    filed_at: datetime
    result: str | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None


class CaseCardOut(CaseOut):
    """`GET /inspections/cases/{id}`: the case's own columns, FLAT, plus its
    append-only timeline, any appeals filed against its decision, and how
    many of the SAME applicant's other cases already reached a decision
    (ruling R8, `tz/04`'s "shows the history" half of the repeat-violation
    line — the "suggests stricter" half is deliberately NOT built)."""

    history: list[CaseHistoryEntry]
    appeals: list[AppealOut]
    prior_cases_count: int

    @classmethod
    def build(cls, card: dict[str, Any]) -> CaseCardOut:
        case = card["case"]
        return cls.model_validate(
            {
                **{name: getattr(case, name) for name in CaseOut.model_fields},
                "history": card["history"],
                "appeals": card["appeals"],
                "prior_cases_count": card["prior_cases_count"],
            }
        )


class ExplanationIn(BaseModel):
    text: ExplanationTextStr
    file_id: uuid.UUID | None = None


class DecisionIn(BaseModel):
    decision: Literal["warning", "suspend", "revoke", "transfer"]
    damage_amount: Decimal | None = Field(default=None, ge=0, le=DAMAGE_AMOUNT_MAX)
    damage_calc: JsonObject | None = None
    note: NoteStr | None = None


class AppealIn(BaseModel):
    text: TextStr


class AppealResolveIn(BaseModel):
    result: TextStr
