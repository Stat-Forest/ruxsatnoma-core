"""API shapes for `public`. `AppealContact` validates the untrusted anonymous
input shape without ever becoming a place the raw text could leak — pydantic's
own `ValidationError` (422 `ERR-VAL-001`, handled centrally) never echoes
field VALUES back for a plain type mismatch, only field names."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, EmailStr, Field, model_validator


class AppealContact(BaseModel):
    """At least one of phone/email — the shared secret `check_appeal_status`
    (R3, `plans/04.6-4.8-public-help.md`) matches against later."""

    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if not self.phone and not self.email:
            raise ValueError("at least one of phone or email is required")
        return self


class AppealIn(BaseModel):
    applicant_name: str = Field(min_length=1, max_length=255)
    contact: AppealContact
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=5000)


class AppealSubmitOut(BaseModel):
    number: str


class AppealStatusOut(BaseModel):
    """Always this shape (R3): an unknown number and a contact mismatch both
    answer `found=False` — one shape for both, the same "no oracle" rule
    `permits.public_router`'s QR check applies to its own miss."""

    found: bool
    status: str | None = None
    subject: str | None = None
    answer_text: str | None = None
    answered_at: datetime | None = None


class AppealAdminOut(BaseModel):
    id: uuid.UUID
    number: str
    applicant_name: str
    contact: dict[str, Any]
    subject: str
    body: str
    status: str
    answer_text: str | None
    answered_by: uuid.UUID | None
    answered_at: datetime | None
    created_at: datetime


class AppealAnswerIn(BaseModel):
    answer_text: str = Field(min_length=1, max_length=5000)


class AppealStatusIn(BaseModel):
    to_status: str = Field(pattern="^(in_progress|closed)$")


class OpenDataLayerOut(BaseModel):
    code: str
    name: dict[str, Any]
    geometry_type: str


class OpenDataOrgStatOut(BaseModel):
    """One row of the k-anonymity-suppressed breakdown (R2) — never emitted
    for a cell below the threshold; the cell is simply absent from the list,
    not present with a zeroed count."""

    organization_id: uuid.UUID
    organization_name: dict[str, Any]
    region_id: uuid.UUID | None
    region_name: dict[str, Any] | None
    active_permits_count: int
    active_area_ha: Decimal


class OpenDataRegionStatOut(BaseModel):
    region_id: uuid.UUID | None
    region_name: dict[str, Any] | None
    active_permits_count: int
    active_area_ha: Decimal


class OpenDataStatsOut(BaseModel):
    k_anonymity_threshold: int
    total_active_permits: int
    total_active_area_ha: Decimal
    by_region: list[OpenDataRegionStatOut]
    by_organization: list[OpenDataOrgStatOut]
