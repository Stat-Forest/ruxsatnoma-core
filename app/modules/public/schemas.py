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


class SiteTextOut(BaseModel):
    """Two languages only: the landing falls back to `uz_latn` for the other
    three UI languages (#90)."""

    uz_latn: str
    ru: str


class SiteSocialOut(BaseModel):
    telegram: str | None = None
    youtube: str | None = None


class SiteContactsOut(BaseModel):
    phone: str
    email: str
    address: SiteTextOut
    hours: SiteTextOut
    social: SiteSocialOut


class SiteSettingsOut(BaseModel):
    """Feeds the landing footer and its season calendar in one anonymous
    request (ruling R3) — an explicit whitelist of `system_settings` keys,
    never a proxy of the store."""

    contacts: SiteContactsOut
    # Ruling R3: provisional until the Agency answers; the strip says so on screen.
    season_windows: dict[str, list[int]]


class RatingSummaryOut(BaseModel):
    """The landing's single national number for citizens' post-issuance
    ratings (#174) — suppressed below `service.OPEN_DATA_K_ANONYMITY`: below
    it `published` is `False` and BOTH `average` and `histogram` are `None`,
    never a number computed from a handful of rows and presented as if it
    meant something nationally. `count` is always the true count, published
    or not — it is what lets the front end say "not enough ratings yet"
    instead of just hiding the block."""

    published: bool
    average: Decimal | None
    count: int
    histogram: dict[int, int] | None
    threshold: int
