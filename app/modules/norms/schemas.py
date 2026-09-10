"""Wire schemas for the parameter/tariff halves of the versioned-number API;
the norm and calculation schemas arrive in Tasks 4 and 7. Values are carried as
**strings**, not floats: `Decimal` is the storage type and a JSON float would
lose the exactness the whole stage is built on."""

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
)


def _benefit_modifiers(value: dict[str, str] | None) -> dict[str, str] | None:
    """Ruling 20's multiplier, bounded (I7, final review). The values reach
    `calculator._apply_benefit` as `tariff.coefficient * Decimal(modifier)`
    with no guard of their own, so `"abc"` was an uncaught `InvalidOperation`
    (a 500) and `"-1"` produced a negative coefficient — a negative `amount`
    that `preview` returned happily and `save_calculation` turned into an
    `amount >= 0` CHECK violation, i.e. another 500. Every other numeric field
    on this schema carries a bound; this one carried none.

    `0 <= modifier <= 1`: a benefit reduces a fee. A multiplier above 1 would
    RAISE it, which is not a benefit under any reading of VMQ 278, so it is
    refused rather than stored.

    Kept as `dict[str, str]` rather than `dict[str, Decimal]` on purpose: the
    column is JSONB and the stock `json.dumps` rejects `Decimal` outright
    (lesson), so the wire and storage form stays the string the calculator
    already parses.

    NOT validated here, and cannot be at this layer: whether the applicant
    CLAIMING a benefit is actually entitled to it. That needs the applicant
    record — stage 3.9's, see `CalculationIn.benefit_code`."""
    if value is None:
        return None
    for code, modifier in value.items():
        try:
            parsed = Decimal(modifier)
            if not (Decimal("0") <= parsed <= Decimal("1")):
                raise ValueError(f"benefit modifier for {code!r} must be between 0 and 1")
        except InvalidOperation as exc:
            # Catches both an unparsable string AND `Decimal("NaN")`, which
            # parses cleanly but makes the bound comparison above raise
            # InvalidOperation rather than return a bool (re-review of the
            # I7 fix wave) — the comparison has to stay inside this `try`.
            raise ValueError(f"benefit modifier for {code!r} is not a number") from exc
    return value


BenefitModifiers = Annotated[dict[str, str] | None, AfterValidator(_benefit_modifiers)]

# The DB CHECKs these mirror live in migrations 0011 (`livestock_group_valid`)
# and 0013 (`quantity_unit_valid`). Unbounded, a typo flushed into an
# `IntegrityError` that `main.py` does not handle — a 500 rather than a 422
# (I8, final review), the same defect class `create_versioned` already guards
# for `activity_type_id`.
#
# Spelled out rather than `Literal[*LIVESTOCK_GROUPS]`: pyright rejects a
# starred VARIABLE in a type expression ("Variable not allowed in type
# expression"), and a `Literal` is exactly the place where a type checker has
# to see the members. That makes these a second copy of the tuples the models
# build their CHECKs from, so — per the lesson on constraint strings
# duplicated in Python tuples — the two sides are asserted equal in
# `tests/modules/norms/test_models.py`, which is the one thing that keeps a
# value added on one side from becoming a 500 on the other.
LivestockGroup = Literal["large_adult", "large_young", "small_adult", "small_young"]
QuantityUnit = Literal["head", "ton", "hive", "ha", "person_day", "m3", "unit"]

# A recurring MM-DD boundary (ruling 14). ASCII class written out rather than
# `\d`, which is Unicode-aware in Python but not in a Postgres CHECK (lesson)
# — the same dialect rule this project applies to every stored pattern.
MonthDay = Annotated[str, Field(pattern=r"^[0-9]{2}-[0-9]{2}$")]


class SeasonWindow(BaseModel):
    """One grazing window. `from` is a Python keyword, so the field is
    `from_` with an alias — which is why `create_norm`/`update_norm` dump
    these `by_alias=True`, keeping the STORED JSONB in the `{"from": ...,
    "to": ...}` shape `checks._in_window` reads."""

    model_config = ConfigDict(populate_by_name=True)

    from_: MonthDay = Field(alias="from")
    to: MonthDay


class Season(BaseModel):
    """`season` used to be free-form JSONB written straight through from the
    request (I5, final review). A window missing `from`/`to` raised a
    `KeyError` INSIDE a check — an uncaught 500 on `POST
    /calculations/preview`, not a domain error — and a non-string value was a
    `TypeError` the same way."""

    windows: list[SeasonWindow] = Field(default_factory=list)


class Rotation(BaseModel):
    """`rest_years` as INTEGERS. Written as strings (`{"rest_years":
    ["2027"]}` — the shape a JSON form happily produces) the rotation check's
    `if year in rest_years` compared an `int` against a `str` and passed
    silently for a resting year: a fail-open on a BLOCKING check from a
    plausible data-entry mistake, with no error anywhere (I5)."""

    rest_years: list[int] = Field(default_factory=list)


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
    livestock_group: LivestockGroup | None = None
    coefficient: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]
    quantity_unit: QuantityUnit
    benefit_modifiers: BenefitModifiers = None
    effective_from: date
    effective_to: date | None = None
    basis: Annotated[str, Field(min_length=1, max_length=500)]


class TariffPatch(BaseModel):
    """`activity_type_id`/`livestock_group` are identity (they are the key
    `service._Versioned.key_filters` matches a tariff by) and stay out of this
    patch for the same reason `RuleParameterPatch` excludes `code`."""

    coefficient: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)] | None = None
    quantity_unit: QuantityUnit | None = None
    benefit_modifiers: BenefitModifiers = None
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
    # Ruling #176 (stage 9): the general capacity limit, in the activity's own
    # `quantity_unit` — grazing keeps `max_sb` alone and refuses this field
    # (`service.create_norm`), so a second source of truth for the same fact
    # can never be written.
    capacity: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)] | None = None
    season: Season | None = None
    rotation: Rotation | None = None
    geobotanic_doc_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None


class NormPatch(BaseModel):
    """`contour_id`/`activity_type_id` are identity and stay out of this patch,
    the same way `TariffPatch` excludes its own key fields."""

    yield_c_per_ha: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)] | None = None
    capacity: Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)] | None = None
    season: Season | None = None
    rotation: Rotation | None = None
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
    capacity: Decimal | None
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
    # measured area): a yield figure — and `capacity`, ruling #176 — is a
    # caller-supplied rate/limit like a coefficient, not a measured quantity,
    # so the response shows the STORED precision rather than echoing the
    # caller's own input shape.
    @field_serializer("yield_c_per_ha", "capacity")
    def _stored_precision_decimal(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


# --- Ruling #177 (stage 9): the leshoz x activity season/minimum-term ------
# dictionary (`activity_seasons`). No lifecycle (draft/review/published/…)
# unlike Norm/Tariff/RuleParameter above — a plain current-value setting a
# leshoz or the central office edits in place, unique on (organization_id,
# activity_type_id) at the database. `season` reuses `Season` verbatim, the
# same malformed-window guard `checks._in_window` already applies at read.


class ActivitySeasonIn(BaseModel):
    organization_id: uuid.UUID
    activity_type_id: uuid.UUID
    season: Season = Field(default_factory=Season)
    min_term_days: Annotated[int, Field(gt=0)] | None = None


class ActivitySeasonPatch(BaseModel):
    """`organization_id`/`activity_type_id` are identity and stay out of this
    patch, the same way `NormPatch` excludes `contour_id`/`activity_type_id`.

    `min_term_days` backs a NULLABLE column — an explicit `null` clears the
    minimum (no minimum enforced), the same `exclude_unset=True` idiom
    `NormPatch.geobotanic_doc_id` already relies on. `season` backs a NOT
    NULL column instead, so an explicit `null` here has no legal meaning —
    to clear the windows a caller sends `{"windows": []}`, a real value, not
    JSON `null` (same reasoning as `OrganizationPatch._reject_explicit_null_
    gis_enabled`)."""

    season: Season | None = None
    min_term_days: Annotated[int, Field(gt=0)] | None = None

    @field_validator("season", mode="after")
    @classmethod
    def _reject_explicit_null_season(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be explicitly cleared")
        return value


class ActivitySeasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    activity_type_id: uuid.UUID
    season: dict[str, Any]
    min_term_days: int | None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class EffectiveSeasonOut(BaseModel):
    """`GET /activity-seasons/effective` — task 4's public read for the
    wizard: what ACTUALLY applies after ruling #177's override resolves,
    through the SAME function the blocking check itself calls
    (`checks.resolve_effective_windows`), so a date picker built from this
    can never disagree with the check that fires if the applicant ignores it.

    `windows` is the raw JSONB list (`{"from": "MM-DD", "to": "MM-DD"}`
    dicts), not `list[SeasonWindow]` — deliberately: a contour's norm may
    predate `schemas.Season`'s edge validation (`checks._in_window`'s own
    docstring), and re-validating its windows through `SeasonWindow` here
    would turn a pre-existing row's already-tolerated malformed window into
    a 500 on a READ endpoint, the opposite of the fail-closed-but-never-
    crashing property this stage exists to preserve.

    `season_source` says WHICH source won: `"norm"` (the contour's own,
    overriding), `"activity_season"` (the leshoz dictionary, the fallback)
    or `"none"` (neither states one — today's unchanged meaning, no
    restriction at all). `min_term_source` is always `"activity_season"` or
    `"none"`: the minimum term has no norm-level override (ruling #177 only
    speaks of overriding the WINDOWS)."""

    activity_type_id: uuid.UUID
    organization_id: uuid.UUID
    contour_id: uuid.UUID | None
    windows: list[dict[str, Any]]
    season_source: Literal["norm", "activity_season", "none"]
    min_term_days: int | None
    min_term_source: Literal["activity_season", "none"]


class PublishWarning(BaseModel):
    """Not `Warning` (M9/deferred minor #7): that name shadows the builtin
    exception class, in a module every other schema in this stage is read
    beside."""

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
    warnings: list[PublishWarning] = []


class LivestockItemIn(BaseModel):
    """One grazing line: how many head of one livestock type. Validity of the
    code itself (a real `livestock_types` entry with a known VMQ 278 group and
    VMQ 689 coefficient) is a snapshot-time question, not a schema one — an
    unknown code surfaces as `ERR-NORM-004` naming the missing parameter,
    exactly like every other missing rule number (ruling 6)."""

    livestock_code: str
    count: Annotated[int, Field(gt=0)]


class CalculationIn(BaseModel):
    """One calculation request, shared by `POST /calculations/preview` and
    `POST /calculations` (`norms.service._compute` is the one path both run
    through). `items` carries grazing's per-group head counts; `quantity` is
    the declared amount for every other activity (VMQ 278's `quantity` is per
    activity — ha for haymaking, m3 for deadwood).

    Deliberately absent: `on_date` (always `business_today()` — a caller
    cannot backdate which rates apply) and `area_ha` (recorded on
    `input_snapshot` from the contour's own published area, never a
    client-declared figure — see `service._compute`)."""

    # OPENED BY STAGE 3.9a (task 5), together — in the same commit — with the
    # two guards that make it safe. It was typed `None` for the whole of 3.7
    # (finding I4, final review) because `POST /calculations` persisted
    # whatever arrived here: no FK, no ownership check, no status check, and
    # `calculations` is append-only, so a row bound to ANY application id could
    # be written by any authenticated user and never deleted or corrected.
    #
    # The three things that had to land with the wider type, and did:
    #
    #   * the FOREIGN KEY — `fk_calculations_application_id_applications`,
    #     shipped by migration 0015 (NOT VALID, then VALIDATE CONSTRAINT, the
    #     way 3.2a closed `audit_log.user_id`), so an id that names nothing is
    #     refused by the database;
    #   * the OWNERSHIP check — the caller must own the target application or
    #     be staff entitled to review it;
    #   * the STATUS check — an application at APPROVED or beyond may not
    #     receive a calculation at all.
    #
    # Both checks live in `service.save_calculation` and NOT in `calc_router`,
    # because `applications.service.submit` is the other caller and would walk
    # straight past a router-level gate. Read the block comment above
    # `save_calculation` for why the status half is a money question rather
    # than a tidiness one: `payments.issue_invoice` and `permits.issue` each
    # read the NEWEST calculation independently and cannot compare notes, and
    # an audit probe on the merged 3.9a/3.10a/3.11a branch had a citizen billed
    # 2 060 000,00 while the permit printed 9 999 999,00.
    #
    # `submit` is still the only path that SHOULD be setting this in 3.9a
    # (ruling 8: exactly one calculation, written at submission); the guards
    # exist because "should" is not a mechanism on a route open to every
    # authenticated user.
    application_id: uuid.UUID | None = None
    contour_id: uuid.UUID
    activity_type_id: uuid.UUID
    period_from: date
    period_to: date
    quantity: Annotated[Decimal, Field(ge=0)] | None = None
    items: list[LivestockItemIn] = Field(default_factory=list)
    benefit_code: str | None = None


class CalculationOut(BaseModel):
    """A saved, immutable calculation (migration 0011's append-only trigger) —
    every column of `norms.models.Calculation`, `Decimal` fields carried as
    strings AT THE COLUMN'S OWN PRECISION (lesson: a fixed-scale NUMERIC
    round-trips at `Numeric(p, s)`'s own scale, not the calculator's — the
    caller must `db.refresh()` the row before this schema ever sees it,
    exactly like `TariffOut.coefficient`/`NormOut.yield_c_per_ha`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID | None
    contour_id: uuid.UUID | None
    activity_type_id: uuid.UUID
    rule_code_version: str
    input_snapshot: dict[str, Any]
    used_sb: Decimal | None
    max_sb: int | None
    remaining_sb: Decimal | None
    amount: Decimal
    breakdown: Any
    created_by: uuid.UUID | None
    created_at: datetime

    @field_serializer("used_sb", "remaining_sb")
    def _nullable_decimal(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)


# --- The public surface (decision #63): a citizen with no session and no ------
# parcel, priced approximately. `PublicEstimateIn`/`PublicEstimateOut` are
# deliberately NOT `CalculationIn`/`CalculationOut`: this door takes no
# `contour_id`, no `application_id` and no `benefit_code`, and nothing it
# returns is ever persisted. See `norms.public_router`/`norms.service.estimate_public`.


class PublicEstimateIn(BaseModel):
    """`POST /public/calculations/estimate`'s request: the activity, the
    declared quantity or — for grazing — per-group head counts shaped exactly
    like `LivestockItemIn`, and the period. No `contour_id` (a random visitor
    names no parcel), no `application_id` and no `benefit_code` (an anonymous
    claim would be unverifiable and is refused by design, not merely unasked)."""

    activity_type_id: uuid.UUID
    period_from: date
    period_to: date
    quantity: Annotated[Decimal, Field(ge=0)] | None = None
    items: list[LivestockItemIn] = Field(default_factory=list)


# The five admissibility checks `checks.run_checks` runs (`checks.BLOCKING`) all
# need a contour — a published norm, a fire-ban/restriction layer, a season or
# rotation window recorded against THAT parcel. An anonymous estimate names
# none, so none of the five ever runs; `PublicEstimateOut.checks_skipped` is
# what states that absence, rather than leaving a reader to assume a bare
# `amount` means everything was checked and came back clean.
SKIPPED_CHECKS: tuple[str, ...] = ("norm", "season", "rotation", "fire_ban", "limit")

PUBLIC_ESTIMATE_DISCLAIMER = (
    "Approximate estimate only — not a binding calculation. No parcel was "
    "selected, so the norm, season, rotation, fire-ban and occupancy-limit "
    "checks did not run, and no benefit was applied. The final amount is set "
    "once a real parcel is chosen inside an application."
)


class PublicEstimateOut(BaseModel):
    """Deliberately NOT `CalculationOut`: nothing here is stored (`calculations`
    is append-only and belongs to a real application), and `approximate=True`
    is a FIELD, not just this docstring — the contract that keeps a front-end
    from rendering the figure as a bill."""

    approximate: Literal[True] = True
    disclaimer: str = PUBLIC_ESTIMATE_DISCLAIMER
    checks_skipped: list[str] = Field(default_factory=lambda: list(SKIPPED_CHECKS))
    activity_type_id: uuid.UUID
    period_from: date
    period_to: date
    quantity: Decimal | None
    items: list[LivestockItemIn]
    amount: Decimal
    used_sb: Decimal | None
    rule_code_version: str
    breakdown: Any

    @field_serializer("amount")
    def _amount(self, value: Decimal) -> str:
        return str(value)

    @field_serializer("quantity", "used_sb")
    def _nullable_decimal(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class PublicActivityTypeOut(BaseModel):
    """A narrowed `admin.schemas.ActivityTypeOut` for `GET
    /public/refs/activity-types`: only what the public catalog needs — `id`,
    `code` (the front-end's own hook for "this is grazing", so it can decide
    whether to render herd inputs), `name`, `description` and
    `processing_days`. Never `quantity_unit`/`status`, which the general,
    authenticated `/refs/*` router already answers and this anonymous
    surface has no reason to repeat. `name` carries whatever languages the
    row has — `uz_latn` since migration `0032`'s backfill (decision #90,
    closing `tz/12` #31's backend half) — returned as-is, never invented.

    `description`/`processing_days` ARE public (ruling #138), unlike
    `quantity_unit`/`status` above: they are the shop-window copy — what the
    landing site shows a citizen deciding which service to apply for — and
    the landing is their only consumer. `description` may be NULL (a row
    with no seeded copy yet, migration `0038`'s own docstring); `processing_days`
    is DISPLAY ONLY, never the enforced deadline (`applications.service.SLA_DAYS`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: dict[str, Any]
    description: dict[str, Any] | None
    processing_days: int


class PublicLivestockTypeOut(BaseModel):
    """Same narrowing as `PublicActivityTypeOut`, for `GET
    /public/refs/livestock-types`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: dict[str, Any]
