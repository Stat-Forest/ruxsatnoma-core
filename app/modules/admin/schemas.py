"""Pydantic schemas for the admin and refs APIs."""

import uuid
from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, Field

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
    is where that belongs."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    code: str
    name: dict[str, Any]
    stir: str | None
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    status: str


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


class ActivityTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: dict[str, Any]
    quantity_unit: str
    status: str
    description: dict[str, Any] | None
    processing_days: int


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
    """Create payload; `kind`/`parent_id` pairing is validated in the service (ruling 6)."""

    parent_id: uuid.UUID | None = None
    kind: str
    code: str
    name: LocalizedName
    stir: Stir | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    requisites: dict[str, Any] = {}


class OrganizationPatch(BaseModel):
    parent_id: uuid.UUID | None = None
    name: LocalizedName | None = None
    stir: Stir | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    requisites: dict[str, Any] | None = None


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
