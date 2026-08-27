"""Pydantic schemas for the admin and refs APIs."""

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel

from app.core.schemas import LocalizedName


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


class ActivityTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: dict[str, Any]
    quantity_unit: str
    status: str


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
    stir: str | None = None
    region_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    requisites: dict[str, Any] = {}


class OrganizationPatch(BaseModel):
    parent_id: uuid.UUID | None = None
    name: LocalizedName | None = None
    stir: str | None = None
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
