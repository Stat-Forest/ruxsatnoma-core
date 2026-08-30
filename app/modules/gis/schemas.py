"""API shapes. GeoJSON is accepted and returned as a plain dict validated by
PostGIS (ST_GeomFromGeoJSON raises on malformed input) — modelling every GeoJSON
variant in pydantic would duplicate a parser we already have in the database."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_serializer

from app.core.schemas import LocalizedName


class LayerOut(BaseModel):
    id: uuid.UUID
    code: str
    name: LocalizedName
    geometry_type: str
    is_public: bool
    style: dict[str, Any]
    status: str


class LayerList(BaseModel):
    items: list[LayerOut]


class LayerPatch(BaseModel):
    style: dict[str, Any] | None = None
    is_public: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class ContourIn(BaseModel):
    """`POST /gis/contours` (design/03): identity only, no geometry — a version
    is drawn separately once the contour exists."""

    layer_id: uuid.UUID
    organization_id: uuid.UUID
    number: str
    kind: str = Field(pattern="^(contour|subcontour)$")
    parent_id: uuid.UUID | None = None


class ContourOut(BaseModel):
    id: uuid.UUID
    layer_id: uuid.UUID
    organization_id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: str
    number: str
    status: str


class ContourPatch(BaseModel):
    """Identity-level housekeeping only (archiving). Geometry changes always go
    through a new version (`POST .../versions`), never through this route."""

    status: str | None = Field(default=None, pattern="^(active|archived)$")


def _trim_decimal(value: Decimal | None) -> str | None:
    """`contour_versions`' area/accuracy columns are fixed-scale NUMERIC, so a
    value like 2.6 round-trips through Postgres as `Decimal('2.6000')` — this
    strips the insignificant trailing zeros before the API returns it.
    `format(value, "f")` forces fixed-point notation first, so this never risks
    `Decimal.normalize()`'s scientific-notation surprise on a whole number
    (`Decimal('100.0000').normalize()` is `Decimal('1E+2')`, not `100`)."""
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class VersionIn(BaseModel):
    """`POST /gis/contours/{id}/versions`. `declared_area_ha` is the source
    file's own figure, kept for reference only — `area_ha` is always computed by
    PostGIS (ruling 2)."""

    geom: dict[str, Any]
    source: str = Field(pattern="^(cadastre|survey|aerial|gps|import)$")
    declared_area_ha: Decimal | None = None
    accuracy_m: Decimal | None = None
    survey_date: date | None = None
    effective_from: date | None = None


class VersionOut(BaseModel):
    id: uuid.UUID
    contour_id: uuid.UUID
    version_no: int
    status: str
    source: str
    area_ha: Decimal
    declared_area_ha: Decimal | None
    accuracy_m: Decimal | None
    survey_date: date | None
    effective_from: date | None

    @field_serializer("area_ha", "declared_area_ha", "accuracy_m")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class VersionPatch(BaseModel):
    """Draft-only metadata edits (service 409s otherwise). Geometry is never
    patched in place — a changed shape is a new version, by design."""

    declared_area_ha: Decimal | None = None
    accuracy_m: Decimal | None = None
    survey_date: date | None = None
    effective_from: date | None = None


class ApproveIn(BaseModel):
    """`POST .../approve`. `approval_doc_id` stays optional HERE (not a
    required field) on purpose: a missing id must reach the caller as this
    module's own `ERR-VAL-001` envelope (`gis.service.approve_version`'s own
    check), not FastAPI's generic request-validation-error body — see the
    task-5 controller's decision on this point."""

    approval_doc_id: uuid.UUID | None = None


def _jsonable_details(value: Any) -> Any:
    """`gis.checks`' `details` can carry a `Decimal` `area_m2` nested inside an
    `items` list (task-4 review, finding 2: the checks module keeps areas as
    `Decimal`, never `float`, per project convention). Pydantic's default JSON
    encoding of a bare value nested inside an `Any`-typed field renders a
    `Decimal` as a quoted STRING — confirmed empirically, and `json_encoders`
    does not reach values nested under `Any` either — which would silently
    turn a number into text on the one response that returns it. Recurse
    rather than special-case the `area_m2` key by name: `details` is
    deliberately left unstructured (deferred to a later review) and may grow
    more Decimal-bearing keys later."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable_details(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable_details(item) for item in value]
    return value


class CheckResultOut(BaseModel):
    """Mirrors `gis.checks.CheckResult` (a TypedDict, not a BaseModel, on the
    Python side) for the one route that returns it over HTTP."""

    check: str
    result: str
    details: dict[str, Any]

    @field_serializer("details")
    def _serialize_details(self, value: dict[str, Any]) -> dict[str, Any]:
        return _jsonable_details(value)


class ChecksOut(BaseModel):
    """`POST /gis/contours/{id}/versions/{vid}/checks`."""

    checks: list[CheckResultOut]
    blocked: bool
