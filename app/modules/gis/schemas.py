"""API shapes. GeoJSON is accepted and returned as a plain dict validated by
PostGIS (ST_GeomFromGeoJSON raises on malformed input) — modelling every GeoJSON
variant in pydantic would duplicate a parser we already have in the database."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, Field, field_serializer, model_validator

from app.core.schemas import LocalizedName
from app.modules.gis import checks


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
    """Identity-level housekeeping: archiving, and the contour/sub-contour
    HIERARCHY. Geometry changes always go through a new version
    (`POST .../versions`), never through this route.

    `kind`/`parent_id` are here because decision #49 ruling 11 puts them here:
    the importer creates every feature flat (`kind='contour'`,
    `parent_id=NULL`) precisely BECAUSE it does not guess a hierarchy from the
    file — the Burchmulla delivery has 92 distinct numbers across 151 features
    — and the hierarchy is set afterwards, by hand, through this route. Stage
    7's data loading depends on it, and until now `ContourPatch` carried
    `status` alone, so an imported contour could never become a sub-contour at
    all.

    `exclude_unset` at the router keeps "not supplied" distinct from
    "explicitly set to null", so `{"parent_id": null}` detaches a sub-contour
    while `{"status": "archived"}` leaves the hierarchy alone.
    """

    status: str | None = Field(default=None, pattern="^(active|archived)$")
    kind: str | None = Field(default=None, pattern="^(contour|subcontour)$")
    parent_id: uuid.UUID | None = None


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
    """`approval_doc_id`/`approved_by`/`published_at` are part of the response
    because a client driving the lifecycle otherwise cannot see WHO approved a
    version or WHEN it went into force — the three facts every one of
    `approve`/`publish`/`return-to-review` turns on. All three are null through
    draft and review, which is exactly what tells an unapproved version from an
    approved one on screen."""

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
    approval_doc_id: uuid.UUID | None
    approved_by: uuid.UUID | None
    published_at: datetime | None

    @field_serializer("area_ha", "declared_area_ha", "accuracy_m")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _trim_decimal(value)


class VersionDetailOut(VersionOut):
    """`GET /gis/contours/{id}/versions/{version_id}` — task defect 4a's other
    half: `VersionOut` alone carries no geometry, so a version id handed over
    out of band still could not actually be looked at. Adds exactly one field
    over the list row."""

    geometry: dict[str, Any]


class VersionPatch(BaseModel):
    """Draft-only metadata edits (service 409s otherwise). Geometry is never
    patched in place — a changed shape is a new version, by design."""

    declared_area_ha: Decimal | None = None
    accuracy_m: Decimal | None = None
    survey_date: date | None = None
    effective_from: date | None = None


class SplitPieceIn(BaseModel):
    """One of the two subcontours `POST /gis/contours/{parent_id}/split`
    produces. Narrowed to what the caller actually decides: the adminka's
    `splitContour.ts` already computed `geom` client-side (decision #91,
    `gis.service.split_contour`'s own docstring on why the cut itself stays
    client-side); everything else about the new contour — `layer_id`,
    `organization_id`, `kind`, `parent_id` — is derived from the parent and is
    never re-typed by the caller the way a plain `POST /gis/contours` would
    require."""

    number: str
    geom: dict[str, Any]
    declared_area_ha: Decimal | None = None


class SplitIn(BaseModel):
    """`POST /gis/contours/{parent_id}/split`. `source`/`accuracy_m`/
    `survey_date`/`effective_from` describe how the split itself was carried
    out — one drawing act, producing both pieces at once — so they are
    supplied ONCE, unlike `declared_area_ha` (each piece's own source-file or
    on-screen figure), which genuinely differs per piece."""

    piece_a: SplitPieceIn
    piece_b: SplitPieceIn
    source: str = Field(pattern="^(cadastre|survey|aerial|gps|import)$")
    accuracy_m: Decimal | None = None
    survey_date: date | None = None
    effective_from: date | None = None


class SplitPieceOut(BaseModel):
    contour: ContourOut
    version: VersionOut


class SplitOut(BaseModel):
    """`POST /gis/contours/{parent_id}/split` response. `parent_id` is echoed
    back for convenience only — the parent's own row is untouched by this call
    (decision #91: it stays exactly as it was, published version included;
    see `gis.service.split_contour`'s own docstring for what that does and
    does not mean for the parent's occupancy and its own topology checks)."""

    parent_id: uuid.UUID
    piece_a: SplitPieceOut
    piece_b: SplitPieceOut


class ApproveIn(BaseModel):
    """`POST .../approve`. `approval_doc_id` stays optional HERE (not a
    required field) on purpose: a missing id must reach the caller as this
    module's own `ERR-VAL-001` envelope (`gis.service.approve_version`'s own
    check), not FastAPI's generic request-validation-error body — see the
    task-5 controller's decision on this point."""

    approval_doc_id: uuid.UUID | None = None


class CheckResultOut(BaseModel):
    """Mirrors `gis.checks.CheckResult` (a TypedDict, not a BaseModel, on the
    Python side) for the one route that returns it over HTTP. `details` can
    carry a `Decimal` `area_m2` and a `uuid.UUID` `feature_id` nested inside
    an `items` list — `checks.jsonable` (shared with `gis.service.
    publish_version`'s `ERR-GIS-003` details, which need the exact same
    conversion for a different, less forgiving reason — see that function's
    own docstring) makes both safe to serialize."""

    check: str
    result: str
    details: dict[str, Any]

    @field_serializer("details")
    def _serialize_details(self, value: dict[str, Any]) -> dict[str, Any]:
        return checks.jsonable(value)


class ChecksOut(BaseModel):
    """`POST /gis/contours/{id}/versions/{vid}/checks`."""

    checks: list[CheckResultOut]
    blocked: bool


def _validate_period(valid_from: date | None, valid_to: date | None) -> None:
    """Shared by `FeatureIn`/`FeaturePatch` below so the comparison itself
    cannot drift between the two — only the pydantic wiring around it differs
    per model (task-6 controller, decision 4: validated here AND by the DB
    CHECK from Task 1, on purpose)."""
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        raise ValueError("valid_to must not be before valid_from")


class FeatureIn(BaseModel):
    """`POST /gis/layers/{code}/features` — a restriction, protection zone or
    fire ban (a period plus a territory: tz/07 items 8/9/14) or any other
    non-contour layer object. `geom` is GeoJSON, parsed by PostGIS the same
    way `VersionIn.geom` is; its TYPE is checked against the layer's own
    declared `geometry_type` in the service (`ERR-VAL-001`,
    reason=geometry_type_mismatch), not here — that check needs the layer
    catalogue row this schema knows nothing about.

    The validity-period check below runs TWICE on purpose (decision 4): here,
    for a same-request 422 with a clear reason instead of a raw DB error; and
    again at the `validity_period_valid` DB CHECK (migration 0010), which is
    what actually guards Task 7's bulk importer — that path never goes
    through this schema at all.
    """

    geom: dict[str, Any]
    organization_id: uuid.UUID | None = None
    name: LocalizedName | None = None
    props: dict[str, Any] = Field(default_factory=dict)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _check_validity_period(self) -> Self:
        _validate_period(self.valid_from, self.valid_to)
        return self


class FeatureOut(BaseModel):
    id: uuid.UUID
    layer_id: uuid.UUID
    organization_id: uuid.UUID | None
    name: dict[str, Any] | None
    props: dict[str, Any]
    valid_from: date | None
    valid_to: date | None
    status: str


class FeaturePatch(BaseModel):
    """Draft-only metadata edits (service 409s otherwise, reason=not_draft —
    the same rule `VersionPatch` already applies to contours): geometry is
    never patched in place, a corrected shape is a new feature. Re-validates
    the validity period when BOTH dates are given in THIS same request; a
    PATCH that only moves one side of an existing period relies on the DB
    CHECK instead (`gis.service.update_feature` catches that `IntegrityError`
    as the same `ERR-VAL-001`, reason=validity_period_invalid)."""

    name: LocalizedName | None = None
    props: dict[str, Any] | None = None
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _check_validity_period(self) -> Self:
        _validate_period(self.valid_from, self.valid_to)
        return self


# --- Task 7: geodata import ---------------------------------------------------


class ImportAccepted(BaseModel):
    """`POST /gis/imports` answers 202 with nothing but the id: the file is
    stored and QUEUED, and the parse happens in the job (ruling 6). Poll
    `GET /gis/imports/{id}` or wait for the `gis.import.finished` notification."""

    import_id: uuid.UUID


class ImportOut(BaseModel):
    """`GET /gis/imports/{id}`. `stats` carries `created` plus the non-blocking
    `warnings` of ruling 7 (area mismatch, duplicate number, organization-name
    mismatch); `error_report` is `[{row, code, message}]` and is only ever
    populated on a `failed` batch — the two are mutually exclusive by
    construction, since an error rolls every write back."""

    id: uuid.UUID
    layer_id: uuid.UUID
    organization_id: uuid.UUID
    file_id: uuid.UUID
    approval_doc_id: uuid.UUID
    format: str
    status: str
    attribute_map: dict[str, Any]
    stats: dict[str, Any] | None
    error_report: list[Any] | None
    created_at: datetime
    finished_at: datetime | None


# --- Task 8: batch publication + the read API for 3.7/3.9 --------------------


class PublishImportOut(BaseModel):
    """`POST /gis/imports/{id}/publish`. `blocked` items are
    `{"version_id": str, "checks": [...]}`; `checks` is already JSON-safe
    (`gis.checks.jsonable`, applied once at `publish_version`'s own
    `ERR-GIS-003` — see that function's docstring for why this must not be
    re-derived here)."""

    published: int
    blocked: list[dict[str, Any]]


class ContourListItem(BaseModel):
    """One row of `GET /gis/contours` — attributes only, no geometry (the
    card, not the list, carries what a picker needs to actually render a
    plot). `occupied_ha`/`s_available_ha`/`occupancy_source` are ruling 14's
    placeholder, shared with `ContourCardOut` below: `s_available_ha`
    degrades to the full `area_ha` until something registers an
    `OCCUPANCY_PROVIDERS` entry, and `occupancy_source` says so explicitly so
    a front-end can never mistake the placeholder for a measurement.

    `s_available_ha` is floored at zero (`gis.service._available_ha`) — a
    negative "available area" is meaningless to a consumer asking how much
    can still be requested. `over_allocated` is the explicit signal for the
    case that floor would otherwise hide: `occupied_ha` already exceeding
    `area_ha` (two permits issued over the whole parcel is a real,
    demo-witnessed state, not a display bug) — named rather than left for a
    reader to notice by subtracting two other fields themselves."""

    id: uuid.UUID
    number: str
    organization_id: uuid.UUID
    area_ha: Decimal
    occupied_ha: Decimal
    s_available_ha: Decimal
    over_allocated: bool
    occupancy_source: str

    @field_serializer("area_ha", "s_available_ha")
    def _serialize_area(self, value: Decimal) -> str | None:
        return _trim_decimal(value)


class ContourCardOut(BaseModel):
    """`GET /gis/contours/{id}` — the published version's own geometry plus
    the same occupancy placeholder `ContourListItem` carries (ruling 14).
    `occupied_ha` is intentionally NOT run through `_trim_decimal`: it is a
    computed sum, not a value round-tripped through a NUMERIC column, and
    keeping its full 4-dp precision (`"0.0000"`, not `"0"`) is what makes it
    read as a real figure rather than a rounded-away one. `s_available_ha`/
    `over_allocated` — see `ContourListItem`'s own docstring, the same shape."""

    id: uuid.UUID
    number: str
    organization_id: uuid.UUID
    kind: str
    version_id: uuid.UUID
    area_ha: Decimal
    geometry: dict[str, Any]
    occupied_ha: Decimal
    s_available_ha: Decimal
    over_allocated: bool
    occupancy_source: str

    @field_serializer("area_ha", "s_available_ha")
    def _serialize_area(self, value: Decimal) -> str | None:
        return _trim_decimal(value)


class FeatureCollectionOut(BaseModel):
    """`GET /gis/layers/{code}/features` — built entirely in SQL
    (`gis.repo.features_geojson`); this schema only shapes what the service
    already assembled and never touches geometry itself.

    `truncated` is a GeoJSON foreign member (legal per RFC 7946) saying that
    the layer holds more than `repo.FEATURE_COLLECTION_LIMIT` matching features
    and this document is a clipped prefix of them. It exists so a client can
    never mistake a capped collection for the whole layer — an uncapped
    `forest_fund` read with no bbox would have serialised the entire fund
    boundary into one response. The fix is to pass a `?bbox=`, and this flag is
    what tells them to."""

    type: str
    truncated: bool = False
    features: list[dict[str, Any]]
