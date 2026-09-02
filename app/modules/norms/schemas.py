"""Wire schemas for the parameter/tariff halves of the versioned-number API;
the norm and calculation schemas arrive in Tasks 4 and 7. Values are carried as
**strings**, not floats: `Decimal` is the storage type and a JSON float would
lose the exactness the whole stage is built on."""

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_serializer


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
    season: Season | None = None
    rotation: Rotation | None = None
    geobotanic_doc_id: uuid.UUID | None = None
    effective_from: date
    effective_to: date | None = None


class NormPatch(BaseModel):
    """`contour_id`/`activity_type_id` are identity and stay out of this patch,
    the same way `TariffPatch` excludes its own key fields."""

    yield_c_per_ha: Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=4)] | None = None
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

    # REFUSED for the whole of 3.7 (I4, final review), and opened by STAGE 3.9
    # — the stage that creates `applications` and with them the ownership this
    # request cannot ask about. `POST /calculations` used to persist whatever
    # arrived here: no FK (ruling 4 defers it), no ownership check, and
    # `calculations` is append-only, so a row bound to ANY application id
    # could be written by any authenticated user and never deleted or
    # corrected. 3.10 builds an invoice from "the newest row for the
    # application", which makes a pre-seeded row a live under-billing vector
    # the moment `applications` exists. Nothing in this stage can validate the
    # id and no legitimate caller has one yet, so it fails closed at the edge
    # rather than staying an undocumented open write.
    #
    # THIS ACCIDENT IS THE ONLY THING CLOSING A LIVE MONEY HOLE, and 3.9 opens
    # it. `payments.issue_invoice` and `permits.issue` each read "the newest
    # calculation for this application" and, being both level 4, cannot compare
    # notes: an audit probe that inserted a newer calculation between invoicing
    # and issuance had the citizen billed 2 060 000,00 while the permit printed
    # 9 999 999,00, with different `calculation_id`s on the invoice and in the
    # permit's immutable snapshot. `service.save_calculation` has NO
    # application-status guard of any kind, and `POST /calculations` reaches it
    # directly with nothing but `get_current_user` — so 3.9b's ruling 17 on
    # `POST /recalculate` does not cover this path. Whoever widens this type
    # lands the guard in `save_calculation` itself, in the same commit.
    # `tests/test_cross_module_journey.py::
    # test_a_calculation_cannot_be_attached_to_an_application_through_the_write_path`
    # is the test that says so; it fails the moment this annotation changes.
    application_id: None = None
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
