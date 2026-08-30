"""Wire schemas for the parameter/tariff halves of the versioned-number API;
the norm and calculation schemas arrive in Tasks 4 and 7. Values are carried as
**strings**, not floats: `Decimal` is the storage type and a JSON float would
lose the exactness the whole stage is built on."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class RuleParameterIn(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+(:[a-z0-9_]+)?$")]
    value: Any
    unit: str | None = None
    effective_from: date
    effective_to: date | None = None
    basis: Annotated[str, Field(min_length=1, max_length=500)]


class RuleParameterPatch(BaseModel):
    value: Any = None
    unit: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    basis: str | None = None


class RuleParameterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    value: Any
    unit: str | None
    effective_from: date
    effective_to: date | None
    basis: str
    status: str
    created_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    created_at: datetime


class TariffIn(BaseModel):
    activity_type_id: uuid.UUID
    livestock_group: str | None = None
    coefficient: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]
    quantity_unit: str
    benefit_modifiers: dict[str, str] | None = None
    effective_from: date
    effective_to: date | None = None
    basis: Annotated[str, Field(min_length=1, max_length=500)]


class TariffPatch(BaseModel):
    """`activity_type_id`/`livestock_group` are identity (they are the key
    `service._Versioned.key_filters` matches a tariff by) and stay out of this
    patch for the same reason `RuleParameterPatch` excludes `code`."""

    coefficient: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)] | None = None
    quantity_unit: str | None = None
    benefit_modifiers: dict[str, str] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    basis: str | None = None


class TariffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    activity_type_id: uuid.UUID
    livestock_group: str | None
    coefficient: Decimal
    quantity_unit: str
    benefit_modifiers: dict[str, str] | None
    effective_from: date
    effective_to: date | None
    basis: str
    status: str

    @field_serializer("coefficient")
    def _coefficient(self, value: Decimal) -> str:
        return str(value)


class NormIn(BaseModel):
    contour_id: uuid.UUID
    activity_type_id: uuid.UUID
    yield_c_per_ha: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)] | None = None
    season: dict[str, Any] | None = None
    rotation: dict[str, Any] | None = None
    geobotanic_doc_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None


class NormPatch(BaseModel):
    """`contour_id`/`activity_type_id` are identity and stay out of this patch,
    the same way `TariffPatch` excludes its own key fields."""

    yield_c_per_ha: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)] | None = None
    season: dict[str, Any] | None = None
    rotation: dict[str, Any] | None = None
    geobotanic_doc_id: uuid.UUID | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class NormApproveIn(BaseModel):
    approval_doc_id: uuid.UUID


class NormOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contour_id: uuid.UUID
    activity_type_id: uuid.UUID
    yield_c_per_ha: Decimal | None
    season: dict[str, Any] | None
    rotation: dict[str, Any] | None
    max_sb: int | None
    geobotanic_doc_id: uuid.UUID | None
    approval_doc_id: uuid.UUID | None
    effective_from: date
    effective_to: date | None
    status: str
    created_by: uuid.UUID
    approved_by: uuid.UUID | None
    published_at: datetime | None
    created_at: datetime

    # Same fixed-scale-NUMERIC lesson as `TariffOut.coefficient` (plain `str`,
    # not the trim-then-rstrip dance `gis.schemas._trim_decimal` uses for a
    # measured area): a yield figure is a caller-supplied rate like a
    # coefficient, not a measured quantity, so the response shows the STORED
    # precision rather than echoing the caller's own input shape.
    @field_serializer("yield_c_per_ha")
    def _yield_c_per_ha(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class Warning(BaseModel):
    code: str
    message: str


class PublishOut(BaseModel):
    """Publication answers with the row AND any risk indicator it raised — RI-04
    for a retroactive effective date (ruling 11). A warning is not an error and
    must not be squeezed into the error envelope.

    `item` is `Any` because it carries either a `RuleParameterOut` or a
    `TariffOut` depending on which route built it — the router converts the
    ORM row with `.model_validate(...)` before putting it here, since an
    `Any`-typed field does not get pydantic's usual from-attributes treatment
    and a raw ORM instance is not JSON-serializable on its own."""

    item: Any
    warnings: list[Warning] = []
