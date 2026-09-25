"""Pydantic schemas for the reports API. Input models are `extra="forbid"`
(the `applications` idiom) so a client field that would silently vanish
raises a 422 naming it instead."""

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas import BlobStr, CodeStr, JsonObject, LocalizedName, TextStr
from app.modules.reports.models import FORM_STATUSES, PERIOD_TYPES, REPORT_STATUSES, RETURNED_BY

# --- Request bounds (stage 17, QA run 01, task 8) -----------------------------
REPORT_FORM_COLUMNS_MAX = 100  # R3
REPORT_FORM_RULES_MAX = 100  # R3
# Raised from 2 000 (M4, final review): a hodim's manual edit
# (`ReportDataUpdate.rows`) and `generate_report`'s own computed row count
# share this cap — a leshoz with more permits/acts than the old 2 000 in one
# reporting period could never generate its own report at all. `reports/
# service.py::generate_report` refuses loudly (`ERR-VAL-001`,
# `reason: "report_too_large"`) if the computed count would exceed it, rather
# than silently truncating the register.
REPORT_ROWS_MAX = 20_000
# `report_forms.version` has no DB constraint of its own (a plain int column);
# this is a named domain constant (R4) — a form is revised by hand a handful
# of times a year, never in the thousands.
REPORT_FORM_VERSION_MAX = 1_000


class ReportFormColumn(BaseModel):
    """One column of a form's `columns` catalog (`tz/13`'s 2-ilova/3-ilova
    tables, as data). `source` says how `service.generate_report` fills a
    row's value for this column — computed off `permits`/`invoices`, or left
    for a hodim to type."""

    model_config = ConfigDict(extra="forbid")

    code: CodeStr
    label: LocalizedName
    source: Literal["auto", "manual"]
    type: Literal["text", "number", "date", "money"]


class ReportFormCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `CodeStr` (C2, final review): stripped, so "F1 " and "F1" are the same
    # code — matches the adminka's own `ReportFormCreateModal.tsx` maxLength.
    code: CodeStr
    version: int = Field(ge=1, le=REPORT_FORM_VERSION_MAX)
    name: LocalizedName
    activity_type_id: uuid.UUID | None = None
    period_type: Literal["month", "quarter", "year"]
    columns: list[ReportFormColumn] = Field(min_length=1, max_length=REPORT_FORM_COLUMNS_MAX)
    # Descriptive only (plan "scope cuts") — what a control ratio IS, for
    # display; `rules.py` is where 2-ilova/3-ilova are actually checked.
    rules: list[JsonObject] = Field(default_factory=list, max_length=REPORT_FORM_RULES_MAX)
    schedule: JsonObject = Field(default_factory=dict)
    valid_from: date | None = None


class ReportFormColumnOut(BaseModel):
    """Same shape as `ReportFormColumn`, but `code` is a plain, unbounded
    `str` (I5, final review): a STORED row may predate `CodeStr`'s 64-char
    bound, and a GET must still list it rather than 500 re-validating
    output through the same tightened type the input schema uses.
    `label: LocalizedName` stays as it is — a data check covers it."""

    code: str
    label: LocalizedName
    source: Literal["auto", "manual"]
    type: Literal["text", "number", "date", "money"]


class ReportFormOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    version: int
    name: dict[str, str]
    activity_type_id: uuid.UUID | None
    period_type: str
    columns: list[ReportFormColumnOut]
    rules: list[dict[str, Any]]
    schedule: dict[str, Any]
    status: str
    valid_from: date | None
    created_at: datetime


class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    form_id: uuid.UUID
    organization_id: uuid.UUID
    period_start: date
    period_end: date


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    form_id: uuid.UUID
    organization_id: uuid.UUID
    period_start: date
    period_end: date
    version_no: int
    parent_report_id: uuid.UUID | None
    status: str
    returned_by: str | None
    data: dict[str, Any]
    filled_by: uuid.UUID | None
    submitted_at: datetime | None
    returned_comment: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    due_at: datetime | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class ReportDataUpdate(BaseModel):
    """A hodim's manual edit — replaces `data["rows"]` wholesale. Row shape is
    intentionally `dict[str, Any]`: the set of columns is the FORM's, not
    fixed in code (plan "scope cuts")."""

    model_config = ConfigDict(extra="forbid")

    rows: list[JsonObject] = Field(max_length=REPORT_ROWS_MAX)


class ReportSignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkcs7: BlobStr


class ReportReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: TextStr


__all__ = [
    "FORM_STATUSES",
    "PERIOD_TYPES",
    "REPORT_STATUSES",
    "RETURNED_BY",
    "ReportCreate",
    "ReportDataUpdate",
    "ReportFormColumn",
    "ReportFormColumnOut",
    "ReportFormCreate",
    "ReportFormOut",
    "ReportOut",
    "ReportReturnIn",
    "ReportSignIn",
]
