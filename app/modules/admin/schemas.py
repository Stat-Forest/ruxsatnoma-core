"""Pydantic schemas for the admin and refs APIs."""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.core.schemas import LocalizedName

# Matches the `stir_format` DB CHECK (ruling 12): catching the shape here means a bad
# value 422s at the schema boundary instead of surfacing as an IntegrityError (500).
# [0-9], not \d: \d is Unicode-aware in Pydantic's pattern matching, so it would
# accept e.g. nine Arabic-Indic digits that the ASCII-only Postgres CHECK rejects.
Stir = Annotated[str, Field(pattern=r"^[0-9]{9}$")]


class RegionOut(BaseModel):
    id: uuid.UUID
    code: str
    soato_code: str | None
    name: dict[str, Any]


class DistrictOut(BaseModel):
    id: uuid.UUID
    code: str
    soato_code: str | None
    name: dict[str, Any]
    region_id: uuid.UUID


class OrganizationOut(BaseModel):
    """The public /refs shape (ruling 10: no permission code, no zone filtering) —
    `requisites` (bank details) is deliberately excluded; the admin write surface
    (Task 5's `OrganizationAdminOut`, gated behind `admin.organizations.manage`)
    is where that belongs.

    `gis_enabled` (decision #178) is here, not only on the admin shape: an
    applicant picking a leshoz needs to know whether to expect a map before
    ever reaching a gis route, and this is the one place every authenticated
    caller already reads an organization's own row."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    code: str
    name: dict[str, Any]
    stir: str | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    status: str
    gis_enabled: bool


class OrganizationAdminOut(BaseModel):
    """The admin write-surface shape (Task 5): `OrganizationOut` plus `requisites` —
    used only by routes gated behind `admin.organizations.manage`, never by /refs."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    code: str
    name: dict[str, Any]
    stir: str | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    requisites: dict[str, Any]
    status: str
    gis_enabled: bool


class ActivityTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: dict[str, Any]
    quantity_unit: str
    status: str
    description: dict[str, Any] | None
    processing_days: int


class ActivityTypePatch(BaseModel):
    """Ruling #139: presentation and the on/off switch. Never `code` (tariffs and
    the calculator resolve by it) and never `quantity_unit` (a CHECK-constrained
    enum the price arithmetic depends on)."""

    name: LocalizedName | None = None
    description: LocalizedName | None = None
    processing_days: int | None = Field(default=None, gt=0)
    sort_order: int | None = None
    status: Literal["active", "archived"] | None = None

    # `processing_days`/`sort_order`/`status` back NOT-NULL columns — unlike
    # `description`, which is genuinely nullable, an explicit `null` for any of
    # these three has no legal meaning. Reject it here (422), same philosophy as
    # the `Stir` pattern above: a bad value 422s at the schema boundary rather
    # than reaching `setattr` and failing the NOT NULL constraint (IntegrityError
    # -> ERR-SYS-001, 500). A field validator only runs when the key is actually
    # present in the payload — an *omitted* field still takes the `None` default
    # and is skipped by `exclude_unset=True` untouched, so this only catches a
    # payload that names the field with a JSON `null`.
    @field_validator("processing_days", "sort_order", "status", mode="after")
    @classmethod
    def _reject_explicit_null(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be explicitly cleared")
        return value


class LivestockTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: dict[str, Any]
    status: str


class ClassifierItemOut(BaseModel):
    id: uuid.UUID
    code: str
    name: dict[str, Any]
    props: dict[str, Any]
    valid_from: date
    valid_to: date | None
    status: str


class OrganizationIn(BaseModel):
    """Create payload; `kind`/`parent_id` pairing is validated in the service (ruling 6).
    `gis_enabled` defaults to the column's own `true` (decision #178) — most creates
    never need to set it explicitly."""

    parent_id: uuid.UUID | None = None
    kind: str
    code: str
    name: LocalizedName
    stir: Stir | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    requisites: dict[str, Any] = {}
    gis_enabled: bool = True


class OrganizationPatch(BaseModel):
    """`gis_enabled` (decision #178) is the central admin's switch: false means this
    leshoz files contours by requisites and shows no map (`gis.service.contour_card`/
    `contour_features_geojson` read it back)."""

    parent_id: uuid.UUID | None = None
    name: LocalizedName | None = None
    stir: Stir | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    requisites: dict[str, Any] | None = None
    gis_enabled: bool | None = None

    # `gis_enabled` backs a NOT NULL column — an explicit JSON `null` has no
    # legal meaning (same reasoning as `ActivityTypePatch._reject_explicit_null`
    # above: an *omitted* field still takes this `None` default and is skipped
    # by `exclude_unset=True` untouched, so this only catches a payload that
    # names the field with a JSON `null`, which would otherwise reach `setattr`
    # and fail the NOT NULL constraint as an unhandled 500).
    @field_validator("gis_enabled", mode="after")
    @classmethod
    def _reject_explicit_null_gis_enabled(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be explicitly cleared")
        return value


class ClassifierIn(BaseModel):
    code: str
    name: LocalizedName


class ClassifierItemIn(BaseModel):
    code: str
    name: LocalizedName
    props: dict[str, Any] = {}
    valid_from: date
    valid_to: date | None = None
    sort_order: int = 0


class ClassifierItemPatch(BaseModel):
    name: LocalizedName | None = None
    props: dict[str, Any] | None = None
    valid_to: date | None = None
    sort_order: int | None = None


class SettingOut(BaseModel):
    key: str
    value: Any
    default: Any
    description: str
    overridden: bool


class SettingIn(BaseModel):
    value: Any
