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
