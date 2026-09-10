"""API shapes for `public`. `AppealContact` validates the untrusted anonymous
input shape without ever becoming a place the raw text could leak — pydantic's
own `ValidationError` (422 `ERR-VAL-001`, handled centrally) never echoes
field VALUES back for a plain type mismatch, only field names."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.core.schemas import LocalizedName


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
    """Feeds the landing footer in one anonymous request (ruling R3) — an
    explicit whitelist of `system_settings` keys, never a proxy of the store.

    `season_windows` used to ride along here (`site_season_windows`'s six
    hard-coded month lists) until the stage 8 fix wave (finding 1) deleted
    that key: it disagreed with the real, per-leshoz windows
    `norms.models.ActivitySeason` and `norms.checks._season_check` had
    started enforcing by the time this branch merged. `GET
    /public/activity-seasons` (`PublicActivitySeasonOut` below) replaces it.

    `rules_url` (stage 10, ruling #184) is the document the applicant accepts
    before signing — `site_rules_url`, edited on the H7 screen — read here
    because the wizard's checkbox links to it and the adminka's public read
    is this route, the same reason the footer's contacts are."""

    contacts: SiteContactsOut
    rules_url: str


class PublicActivitySeasonOut(BaseModel):
    """One activity's effective season with no leshoz specified — `GET
    /public/activity-seasons` (stage 8 fix wave finding 1, supersedes the R3
    half of decision #175).

    `windows` is the raw JSONB list `norms.checks.resolve_effective_windows`
    returns (`{"from": "MM-DD", "to": "MM-DD"}` dicts, `norms.schemas.
    EffectiveSeasonOut`'s own shape) — never re-validated into a stricter
    model here, for the identical reason that route gives: a pre-existing
    row may predate the window's own edge validation, and turning an already
    tolerated malformed window into a 500 on a READ endpoint would be worse
    than showing it as-is.

    With no leshoz named, the dictionary this anonymous route consults is
    the AGENCY's own `activity_seasons` rows — the nationwide default
    (2026-09-10). An activity the Agency has no row for answers `[]` and
    `"none"`: nothing is configured to show, never "open all year".
    `is_default` marks that on every row: a real leshoz's own window, reached
    through the authenticated `GET /activity-seasons/effective`
    (`norms.service.effective_season`), overrides it for that leshoz."""

    activity_type_code: str
    windows: list[dict[str, Any]]
    season_source: str
    is_default: bool


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


class ApplicationStatusOut(BaseModel):
    """`GET /public/applications/check` — status without logging in (task 4).

    Same "no oracle" posture as `AppealStatusOut`: an unknown `number` and a
    `number` whose `phone` does not match answer identically, every field
    `None` but `found`. What this shape may NEVER carry — the applicant's
    name, the contour geometry, the calculated sum, attachments, the
    reviewing official — stays behind the cabinet login; only the status,
    its human label, the activity, the leshoz and what happens next cross
    this boundary.
    """

    found: bool
    number: str | None = None
    status: str | None = None
    status_label: LocalizedName | None = None
    activity_type: str | None = None
    organization: str | None = None
    next_step: str | None = None
    submitted_at: date | None = None
