"""The rule engine's arithmetic. Pure, synchronous, Decimal-only.

Formulas (tz/06, corrected against VMQ 689 by plan 03.7 ruling 5):
    Oz          = yield_c_per_ha × area_ha × season_share
    Oz_eff      = Oz × safety_reserve                       (0.85, the 15% weather reserve)
    MaxSB       = round_heads(Oz_eff / sb_feed_norm)        (3.74 c of feed units per head;
                                                              `rounding_heads` decides floor vs
                                                              half-up — ruling 19 defaults it to
                                                              floor, never up, but the MODE itself
                                                              is a parameter like any other)
    UsedSB      = Σ count_i × coef_sb:<code_i>
    RemainingSB = round_heads(MaxSB − committed load)       (LOAD_PROVIDERS, ruling 12;
                                                              rounded like MaxSB — ruling 19
                                                              rounds neither limit in the
                                                              applicant's favour)
    Amount      = БҲМ × coefficient × quantity              (VMQ 278; the unit is per activity)

`RULE_CODE_VERSION` is the only hard-coded value here, and it is not a quantity:
it is the identity of THIS arithmetic, stored on every calculation so a row made
today can be explained years from now. Bump it whenever a formula changes shape —
never when a parameter's value changes, which is what the effective periods are for.

Two rules govern every function below: no I/O (this module never sees a
session — `params.py` is the only thing that queries) and no literal
quantities (every number comes from `snapshot.values`/`ParamSnapshot`, and a
missing one raises `ERR-NORM-004` naming itself rather than falling back to a
default)."""

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.core.errors import err

RULE_CODE_VERSION = "norms-1.0.0"

GRAZING = "grazing"


@dataclass(frozen=True)
class LivestockItem:
    livestock_code: str
    count: int


@dataclass(frozen=True)
class CalcRequest:
    """What a caller is asking for. `items` carries per-group head counts for
    grazing; `quantity` is the declared amount for every other activity (VMQ
    278's `quantity` is per-activity — ha for haymaking, m3 for deadwood, and
    so on). `area_ha` is the requested plot area, recorded for the audit trail
    (`input_snapshot`) — it plays no part in `Amount` itself, since a
    grazing/haymaking norm's own `max_sb`/rate is already resolved by the time
    a calculation runs."""

    activity_code: str
    on_date: date
    period_from: date
    period_to: date
    area_ha: Decimal
    items: tuple[LivestockItem, ...]
    quantity: Decimal | None
    benefit_code: str | None


@dataclass(frozen=True)
class TariffFact:
    """One VMQ 278 rate row, as the calculator needs it — a plain fact, not an
    ORM row, so this module never has to import `norms.models`."""

    id: uuid.UUID | None
    livestock_group: str | None
    coefficient: Decimal
    quantity_unit: str
    benefit_modifiers: dict[str, str] | None


@dataclass(frozen=True)
class NormFact:
    """The published norm in force, if any — `max_sb` is already frozen (Task
    4's `publish_norm`), never recomputed here.

    `capacity` (ruling #176, stage 9) is the generalised limit for every
    activity BUT grazing, in that activity's own `quantity_unit` — a trailing,
    defaulted field so every existing construction of this dataclass (tests
    included) that never mentions it keeps meaning exactly what it meant
    before. Grazing keeps reading `max_sb` alone; `resolve_capacity` below is
    the one place that picks between the two."""

    id: uuid.UUID | None
    yield_c_per_ha: Decimal | None
    max_sb: int | None
    season: dict[str, Any] | None
    rotation: dict[str, Any] | None
    capacity: Decimal | None = None


@dataclass(frozen=True)
class ParamSnapshot:
    """Everything `calculate` needs, already resolved from the database by
    `params.load_snapshot` — the calculator itself never queries anything.

    The four trailing fields are ruling #176's admissibility facts, NOT
    pricing inputs: `calculate` below never reads them (only
    `checks._limit_check` does), which is why they carry no counterpart in
    `CalcResult`/`input_snapshot` and `from_input_snapshot`'s reconstruction
    never has to set them — their defaults are exactly what a calculation
    computed years ago, before this stage, would have meant. `capacity_load`/
    `capacity_load_source` mirror `load_sb`/`load_source` for every activity
    but grazing (`service.CAPACITY_LOAD_PROVIDERS`); `occupied_until`/
    `occupied_until_source` answer the EXCLUSIVE case — no capacity at all —
    with the day an overlapping active permit still covers, or `None`, never
    a bare boolean (`service.EXCLUSIVITY_PROVIDERS`)."""

    values: Mapping[str, Any]
    tariffs: tuple[TariffFact, ...]
    norm: NormFact | None
    load_sb: Decimal
    load_source: str
    capacity_load: Decimal = Decimal("0")
    capacity_load_source: str = "none"
    occupied_until: date | None = None
    occupied_until_source: str = "none"
    # The activity's own `activity_types.quantity_unit`, carried so a capacity
    # refusal can say WHAT it counted (integration finding, stage 9 wave 1 —
    # «40 of 100» with no unit is unreadable). Deliberately NOT taken from the
    # tariff rows beside it: an activity may lawfully have no tariff at all
    # (science, ruling in `tz/06`), and it still has a unit. `None` when the
    # snapshot was built without one — every pre-stage-9 construction, tests
    # included — and the refusal then states no unit rather than inventing one.
    quantity_unit: str | None = None


@dataclass(frozen=True)
class CalcResult:
    amount: Decimal
    used_sb: Decimal | None
    max_sb: int | None
    remaining_sb: Decimal | None
    breakdown: list[dict[str, Any]]
    rule_code_version: str
    input_snapshot: dict[str, Any]


def jsonable(value: Any) -> Any:
    """Recursively converts a snapshot/details value into something a plain
    `json.dumps` can serialize — `Decimal` -> `str` (never `float`: money and
    norms are `Decimal` everywhere in this project, and `str(Decimal(...))`
    round-trips exactly, which a float would not), `date`/`datetime` -> ISO
    `str`, `uuid.UUID` -> `str`, recursing through `dict`/`list`/`tuple`;
    everything else passes through unchanged. The template is
    `gis.checks.jsonable`, except that one converts `Decimal` to `float` for
    an area figure where exactness does not matter — money and norm
    quantities here always do (lesson: `DomainError`'s JSON response has no
    encoder of its own, and a JSONB column fed by the stock `json.dumps`
    rejects `Decimal`/`date` outright)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


def _param(values: Mapping[str, Any], code: str) -> str:
    """A parameter, or a self-naming ERR-NORM-004. Never a default (ruling 6)."""
    if code not in values:
        raise err("ERR-NORM-004", details={"code": code})
    return values[code]


def _decimal(values: Mapping[str, Any], code: str) -> Decimal:
    try:
        return Decimal(str(_param(values, code)))
    except InvalidOperation as exc:  # a parameter published with a non-numeric value
        raise err("ERR-NORM-004", details={"code": code}) from exc


def _rule(values: Mapping[str, Any], code: str) -> Mapping[str, Any]:
    """Like `_param`, but for a value stored as a JSON object rather than the
    plain string every numeric parameter uses — migration 0012 inserts
    `rounding_money`/`rounding_heads` via `CAST(:value AS jsonb)`, not
    `to_jsonb(CAST(:value AS text))`, so they come back as a `dict`, never a
    `str`. `_param`'s own `-> str` annotation would be the wrong shape to
    reuse here (and a real pyright mismatch against `_round_money`'s and
    `_round_heads`'s `Mapping[str, Any]` parameter) — same self-naming
    ERR-NORM-004, different declared type."""
    if code not in values:
        raise err("ERR-NORM-004", details={"code": code})
    return values[code]


def _round_money(amount: Decimal, rule: Mapping[str, Any]) -> Decimal:
    """ROUND_HALF_UP, not Python's default ROUND_HALF_EVEN: tz/06 says ≥0.5 goes
    up, and a banker's-rounding sum would be a cent off in a way nobody can
    explain to an accountant.

    `mode` and `step` are both REQUIRED (fix-round 1, finding 4): this used to
    default a missing `step` to 1 and silently take the floor branch for
    ANY mode other than `"half_up"` — so an admin's `{"mode": "HALF_UP"}`
    typo (wrong case) or a `rounding_money` republished with no `step` at all
    floored the 0.5-rounds-up boundary DOWN to 0 with no error, defeating the
    one test written to prevent exactly that. An unrecognised mode or a
    missing step is now the same self-naming ERR-NORM-004 as every other
    missing/invalid parameter, never a silent default."""
    if "step" not in rule:
        raise err("ERR-NORM-004", details={"code": "rounding_money"})
    step = Decimal(str(rule["step"]))
    mode = rule.get("mode")
    if mode == "half_up":
        return (amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    if mode == "floor":
        return (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step
    raise err("ERR-NORM-004", details={"code": "rounding_money"})


def _round_heads(value: Decimal, rule: Mapping[str, Any]) -> int:
    """Applies `rounding_heads` to the feed-stock quotient inside `max_sb` —
    `"floor"` (ruling 19: a limit is never rounded in the applicant's favour)
    or `"half_up"`, nothing else.

    Fix-round 1, finding 3: `rounding_heads` used to be loaded into every
    snapshot and never read at all — half of "rounding is a parameter" was
    silently unenforced. An unrecognised mode is exactly as dangerous here as
    it is for money (a limit rounded the wrong way silently over- or
    under-grants grazing capacity), so it raises the same self-naming
    ERR-NORM-004 rather than defaulting."""
    mode = rule.get("mode")
    if mode == "floor":
        return int(value.to_integral_value(ROUND_FLOOR))
    if mode == "half_up":
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    raise err("ERR-NORM-004", details={"code": "rounding_heads"})


def _fmt6(value: Decimal) -> str:
    """A coefficient formatted at `tariffs.coefficient`'s own column precision
    (`numeric(12,6)`) — the same fixed-scale convention the lesson on NUMERIC
    round-tripping applies to a value read back from Postgres, extended here
    to a value this module computed itself (a benefit-adjusted coefficient
    never touches the database at all)."""
    return format(value, ".6f")


def _resolve_group_tariff(tariffs: tuple[TariffFact, ...], group: str) -> TariffFact | None:
    for tariff in tariffs:
        if tariff.livestock_group == group:
            return tariff
    return None


def _resolve_flat_tariff(tariffs: tuple[TariffFact, ...]) -> TariffFact | None:
    for tariff in tariffs:
        if tariff.livestock_group is None:
            return tariff
    return None


def _is_tariff_exempt(values: Mapping[str, Any], activity_code: str) -> bool:
    """Is this activity genuinely UN-TARIFFED by law (`science` today), as
    opposed to missing a row it ought to have?

    "The law is silent here" is a FACT that has to be stated, not the fallback
    for "no row found" — and, like every other fact this engine uses, it is a
    versioned `rule_parameters` row (`tariff_exempt:<activity_code>`, seeded
    published by migration 0013) rather than a constant in this file, so a new
    un-tariffed activity is a row with an effective period, not a deploy
    (ruling 6). Fail-closed on the value: only the literal `true` exempts, so
    a row left behind as `"false"` once an activity acquires a rate — or a
    typo — leaves the activity billable and its missing tariff loud."""
    raw = values.get(f"tariff_exempt:{activity_code}")
    return raw is not None and str(raw).strip().lower() == "true"


def _apply_benefit(
    tariff: TariffFact, benefit_code: str | None
) -> tuple[Decimal, dict[str, Any] | None]:
    """The coefficient after a claimed benefit, and the breakdown line that
    records it — or `(tariff.coefficient, None)` unchanged when none is
    claimed, OR when this particular row does not itself define it.

    Whether the CLAIM as a whole is legitimate is `_check_benefit_claim`'s
    job, called once up front against every row `calculate` resolved for this
    request. By the time this runs, the code is already known to be real for
    the request as a whole, so a row that happens not to grant it (a benefit
    that only some livestock groups carry, say) is not an error here — just a
    no-op for that one line, never a silent full-price charge for the request
    as a whole (ruling 20)."""
    if benefit_code is None:
        return tariff.coefficient, None
    modifiers: dict[str, str] = tariff.benefit_modifiers or {}
    if benefit_code not in modifiers:
        return tariff.coefficient, None
    modifier = str(modifiers[benefit_code])
    coefficient_after = tariff.coefficient * Decimal(modifier)
    line = {
        "kind": "benefit",
        "code": benefit_code,
        "modifier": modifier,
        "coefficient_before": _fmt6(tariff.coefficient),
        "coefficient_after": _fmt6(coefficient_after),
    }
    return coefficient_after, line


def _check_benefit_claim(benefit_code: str | None, tariffs: list[TariffFact]) -> None:
    """Ruling 20, closed fully (fix-round 1, finding 1): validates a claimed
    benefit ONCE, up front, against the UNION of every tariff row `calculate`
    actually resolved for this request — never deferred to `_apply_benefit`,
    which only ever sees rows that already exist and so could not catch a
    code that reaches no row at all. Three paths used to accept a bogus code
    with no error at all, silently recording it in `input_snapshot` as if it
    had been honoured: grazing with an empty `items`, a flat activity with no
    tariff row (`science`), and a grazing group whose own tariff row is
    missing (now closed structurally — that case raises `ERR-NORM-004` in
    `calculate` before this function is even reached). A code valid for AT
    LEAST ONE resolved row is accepted for the whole request; `_apply_benefit`
    then decides per row whether it actually applies there."""
    if benefit_code is None:
        return
    known: set[str] = set()
    for tariff in tariffs:
        known.update((tariff.benefit_modifiers or {}).keys())
    if benefit_code not in known:
        raise err("ERR-VAL-001", details={"reason": "unknown_benefit_code", "code": benefit_code})


def max_sb(*, area_ha: Decimal, yield_c_per_ha: Decimal, params: Mapping[str, Any]) -> int:
    oz = yield_c_per_ha * area_ha * _decimal(params, "season_share")
    oz_eff = oz * _decimal(params, "safety_reserve")
    quotient = oz_eff / _decimal(params, "sb_feed_norm")
    return _round_heads(quotient, _rule(params, "rounding_heads"))


def remaining_sb(max_sb_value: int, load_sb: Decimal, params: Mapping[str, Any]) -> Decimal:
    """The limit left on a contour: MaxSB minus the load already committed
    against it, rounded by `rounding_heads` like every other limit.

    Ruling 19 names `remaining_sb` next to `max_sb` — "never round a limit in
    the applicant's favour" — but only `max_sb` was ever rounded (I1, final
    review). Invisible while `LOAD_PROVIDERS` is empty and `load_sb` is
    always 0; the moment 3.11 registers a real provider the load turns
    fractional (the `coef_sb` scale runs down to 0.2 for a lamb) and an
    unfloored remainder hands the applicant the fraction of a conditional
    head the ruling says to take away.

    THE one place that computes it: `calculate` below and
    `checks._limit_check` both call this rather than each subtracting for
    itself, so the number an applicant is refused by and the number stored on
    the calculation can never drift apart. Returns a `Decimal` (integral in
    value) because `Calculation.remaining_sb` is a `NUMERIC(12,4)` column."""
    return Decimal(_round_heads(Decimal(max_sb_value) - load_sb, _rule(params, "rounding_heads")))


def resolve_capacity(activity_code: str, norm: NormFact | None) -> Decimal | None:
    """Ruling #176: the ONE place that picks between grazing's `max_sb` and
    every other activity's own `capacity` — `checks._limit_check` calls this
    rather than re-deriving the branch itself, and `params.load_snapshot`
    calls it too, to decide whether it needs the capacity-load seam or the
    exclusivity one.

    `None` here is a FACT the caller acts on, not an error: no norm at all,
    or the relevant column left unset, both mean "nothing to compare
    against" — ruling #176's EXCLUSIVE case, never "unlimited". Wrapped as a
    `Decimal` even for grazing's integer `max_sb` so every caller subtracts
    a committed load the same way regardless of activity."""
    if norm is None:
        return None
    if activity_code == GRAZING:
        return None if norm.max_sb is None else Decimal(norm.max_sb)
    return norm.capacity


def from_input_snapshot(input_snapshot: Mapping[str, Any]) -> tuple[CalcRequest, ParamSnapshot]:
    """Rebuilds the exact `(request, snapshot)` pair a stored calculation was
    computed from, using nothing but its own `input_snapshot` column.

    This is what ruling 18's promise MEANS — "sufficient to recompute it years
    later" — turned from a claim into something executable: feed the result
    back through `calculate` and the same `amount`/`used_sb`/`max_sb`/
    `remaining_sb` must come out. Until I9 (final review) nothing proved it;
    the tests asserted only that particular KEYS were present, which is how
    the missing `load_sb`/`load_source` went unnoticed until fix-round 1.

    It is a VERIFICATION tool, not a pricing path. The public-surface note at
    the end of `service.py` still stands: a level-4+ caller must never
    recompute a stored `Calculation`'s amount — a saved row is already the
    answer, and `rule_code_version` names the arithmetic that produced it.
    Reading a snapshot written under a DIFFERENT `RULE_CODE_VERSION` than this
    module's would recompute it under today's formula shape, which is exactly
    the thing that would silently disagree with the stored figure.

    Every value comes back as the string `jsonable` produced; `_decimal`/
    `_rule` accept those forms, and the conversions below cover the fields
    whose TYPE (not merely precision) has to be restored — the dates, the
    `Decimal` quantities and the `uuid.UUID` identifiers."""
    raw_request = input_snapshot["request"]
    request = CalcRequest(
        activity_code=raw_request["activity_code"],
        on_date=date.fromisoformat(raw_request["on_date"]),
        period_from=date.fromisoformat(raw_request["period_from"]),
        period_to=date.fromisoformat(raw_request["period_to"]),
        area_ha=Decimal(raw_request["area_ha"]),
        items=tuple(
            LivestockItem(item["livestock_code"], int(item["count"]))
            for item in raw_request["items"]
        ),
        quantity=None if raw_request["quantity"] is None else Decimal(raw_request["quantity"]),
        benefit_code=raw_request["benefit_code"],
    )
    tariffs = tuple(
        TariffFact(
            id=None if row["id"] is None else uuid.UUID(row["id"]),
            livestock_group=row["livestock_group"],
            coefficient=Decimal(row["coefficient"]),
            quantity_unit=row["quantity_unit"],
            benefit_modifiers=row["benefit_modifiers"],
        )
        for row in input_snapshot["tariffs"]
    )
    raw_norm = input_snapshot["norm"]
    norm = (
        None
        if raw_norm is None
        else NormFact(
            id=None if raw_norm["id"] is None else uuid.UUID(raw_norm["id"]),
            yield_c_per_ha=(
                None if raw_norm["yield_c_per_ha"] is None else Decimal(raw_norm["yield_c_per_ha"])
            ),
            max_sb=raw_norm["max_sb"],
            season=raw_norm["season"],
            rotation=raw_norm["rotation"],
            # `.get`, not `[...]`: a calculation stored before ruling #176
            # (stage 9) has no "capacity" key at all in its frozen JSONB, and
            # `calculations` is append-only — that row must still reconstruct.
            capacity=(None if raw_norm.get("capacity") is None else Decimal(raw_norm["capacity"])),
        )
    )
    snapshot = ParamSnapshot(
        values=input_snapshot["params"],
        tariffs=tariffs,
        norm=norm,
        load_sb=Decimal(input_snapshot["load_sb"]),
        load_source=input_snapshot["load_source"],
    )
    return request, snapshot


def calculate(request: CalcRequest, snapshot: ParamSnapshot) -> CalcResult:
    """Resolves the tariff (for grazing, one per livestock group via
    `tariff_group:<code>`; otherwise the single row with `livestock_group IS
    NULL`), applies a claimed benefit modifier, multiplies, sums, rounds once
    at the end, and computes `used_sb` for any grazing request — a herd's
    conditional-head load is a fact about the REQUEST (`request.items` ×
    `coef_sb:<code>`), never about whether a norm happens to exist yet — plus
    `max_sb`/`remaining_sb` when a norm IS present (those two, unlike
    `used_sb`, are genuinely a NORM fact: `snapshot.norm.max_sb` and the
    committed load against it). `breakdown` carries one line per tariff row
    applied, plus a `benefit` line per claimed-and-honoured benefit and a
    `limit` line when a norm is present — every `Decimal` already stringified
    by `jsonable`. `input_snapshot` is complete enough to recompute this exact
    row years from now without the database."""
    values = snapshot.values
    bhm = _decimal(values, "bhm")
    breakdown: list[dict[str, Any]] = []
    total = Decimal("0")

    if request.activity_code == GRAZING:
        resolved: list[tuple[LivestockItem, str, TariffFact]] = []
        for item in request.items:
            group = _param(values, f"tariff_group:{item.livestock_code}")
            tariff = _resolve_group_tariff(snapshot.tariffs, group)
            if tariff is None:
                # VMQ 278 has a published rate for all four grazing groups, so
                # a missing row here is a GAP in our own tariff table (an
                # archived row whose replacement has not started yet — legal
                # under the EXCLUDE index), never the law being silent.
                # Billing zero under "no_tariff_by_law" would be silent
                # under-billing with the audit trail asserting lawfulness —
                # fix-round 1, finding 2.
                raise err(
                    "ERR-NORM-004",
                    details={"code": f"tariff:{request.activity_code}:{group}"},
                )
            resolved.append((item, group, tariff))

        # Validated ONCE, up front, against every row this request actually
        # resolved — fix-round 1, finding 1 (an empty `items` used to let a
        # bogus code through with no error at all).
        _check_benefit_claim(request.benefit_code, [tariff for _, _, tariff in resolved])

        for item, group, tariff in resolved:
            coefficient, benefit_line = _apply_benefit(tariff, request.benefit_code)
            line_amount = bhm * coefficient * item.count
            total += line_amount
            breakdown.append(
                {
                    "kind": "tariff",
                    "livestock_code": item.livestock_code,
                    "group": group,
                    "coefficient": _fmt6(tariff.coefficient),
                    "count": item.count,
                    "quantity_unit": tariff.quantity_unit,
                    "amount": jsonable(line_amount),
                }
            )
            if benefit_line is not None:
                benefit_line["livestock_code"] = item.livestock_code
                breakdown.append(benefit_line)
    else:
        tariff = _resolve_flat_tariff(snapshot.tariffs)
        # Same up-front validation as the grazing branch, against whatever
        # was (or was not) resolved — fix-round 1, finding 1's second leak
        # path: `science` (no tariff row at all) used to accept any bogus
        # code silently.
        _check_benefit_claim(request.benefit_code, [tariff] if tariff is not None else [])
        if tariff is None:
            # C2 (final review): "the law is silent here" is an explicit,
            # versioned FACT, never the fallback for "no row found". VMQ 278
            # genuinely has no rate for `science` — contracted separately —
            # but it DOES publish one for haymaking, apiary, deadwood and
            # recreation, and changing any of those requires
            # archive-then-republish, so a window with no effective row is
            # reachable through the documented workflow. Billing zero there
            # would be exactly the silent under-billing the grazing branch
            # above refuses, with the audit trail asserting lawfulness, on an
            # append-only row an invoice (3.10) is later built from.
            if not _is_tariff_exempt(values, request.activity_code):
                raise err(
                    "ERR-NORM-004",
                    details={"code": f"tariff:{request.activity_code}"},
                )
            breakdown.append(
                {
                    "kind": "tariff",
                    "reason": "no_tariff_by_law",
                    "activity_code": request.activity_code,
                    "amount": jsonable(Decimal("0")),
                }
            )
        else:
            if request.quantity is None:
                raise err("ERR-VAL-001", details={"reason": "quantity_required"})
            coefficient, benefit_line = _apply_benefit(tariff, request.benefit_code)
            line_amount = bhm * coefficient * request.quantity
            total += line_amount
            breakdown.append(
                {
                    "kind": "tariff",
                    "activity_code": request.activity_code,
                    "coefficient": _fmt6(tariff.coefficient),
                    "quantity": jsonable(request.quantity),
                    "quantity_unit": tariff.quantity_unit,
                    "amount": jsonable(line_amount),
                }
            )
            if benefit_line is not None:
                benefit_line["activity_code"] = request.activity_code
                breakdown.append(benefit_line)

    amount = _round_money(total, _rule(values, "rounding_money"))

    used_sb: Decimal | None = None
    max_sb_value: int | None = None
    # Named `..._value` for the same reason `max_sb_value` is: the module-level
    # `remaining_sb`/`max_sb` functions are the shared formulas, and a local of
    # the same name would shadow them inside this function.
    remaining_sb_value: Decimal | None = None
    # `used_sb` depends only on `request.items`/`snapshot.values` — it reads
    # nothing from `snapshot.norm` — so it is gated on the ACTIVITY, not on
    # whether a norm happens to be on record for this contour yet (fix-round
    # 2: a request's own conditional-head load must be knowable, and a
    # missing `coef_sb:<code>` must raise, even before VMQ 689's norm for
    # this contour exists — bundling this under `if snapshot.norm is not
    # None` used to force any caller needing that number in the no-norm case
    # to duplicate this exact resolution itself).
    if request.activity_code == GRAZING:
        used_sb = Decimal("0")
        for item in request.items:
            coefficient = _decimal(values, f"coef_sb:{item.livestock_code}")
            used_sb += coefficient * item.count
    # `max_sb`/`remaining_sb` genuinely ARE a norm fact (the limit itself, and
    # the committed load against it) and stay gated on the norm's presence.
    if snapshot.norm is not None:
        max_sb_value = snapshot.norm.max_sb
        if max_sb_value is not None:
            remaining_sb_value = remaining_sb(max_sb_value, snapshot.load_sb, values)
        breakdown.append(
            {
                "kind": "limit",
                "used_sb": jsonable(used_sb),
                "max_sb": max_sb_value,
                "remaining_sb": jsonable(remaining_sb_value),
                "load_sb": jsonable(snapshot.load_sb),
                "load_source": snapshot.load_source,
            }
        )

    input_snapshot = {
        "request": jsonable(asdict(request)),
        "params": jsonable(dict(values)),
        "tariffs": [jsonable(asdict(t)) for t in snapshot.tariffs],
        "norm": jsonable(asdict(snapshot.norm)) if snapshot.norm is not None else None,
        "rule_code_version": RULE_CODE_VERSION,
        # Fix-round 1, finding 6 (controller ruling): remaining_sb = max_sb -
        # load_sb, so without these two the snapshot alone cannot reproduce
        # that one number years later — the whole point of this column.
        "load_sb": jsonable(snapshot.load_sb),
        "load_source": snapshot.load_source,
    }

    return CalcResult(
        amount=amount,
        used_sb=used_sb,
        max_sb=max_sb_value,
        remaining_sb=remaining_sb_value,
        breakdown=breakdown,
        rule_code_version=RULE_CODE_VERSION,
        input_snapshot=input_snapshot,
    )
