"""The rule engine's arithmetic. Pure, synchronous, Decimal-only.

Formulas (tz/06, corrected against VMQ 689 by plan 03.7 ruling 5):
    Oz          = yield_c_per_ha × area_ha × season_share
    Oz_eff      = Oz × safety_reserve                       (0.85, the 15% weather reserve)
    MaxSB       = floor(Oz_eff / sb_feed_norm)              (3.74 c of feed units per head)
    UsedSB      = Σ count_i × coef_sb:<code_i>
    RemainingSB = MaxSB − already-committed load            (LOAD_PROVIDERS, ruling 12)
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
    4's `publish_norm`), never recomputed here."""

    id: uuid.UUID | None
    yield_c_per_ha: Decimal | None
    max_sb: int | None
    season: dict[str, Any] | None
    rotation: dict[str, Any] | None


@dataclass(frozen=True)
class ParamSnapshot:
    """Everything `calculate` needs, already resolved from the database by
    `params.load_snapshot` — the calculator itself never queries anything."""

    values: Mapping[str, Any]
    tariffs: tuple[TariffFact, ...]
    norm: NormFact | None
    load_sb: Decimal
    load_source: str


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
    reuse here (and a real pyright mismatch against `_round_money`'s
    `Mapping[str, Any]` parameter) — same self-naming ERR-NORM-004, different
    declared type."""
    if code not in values:
        raise err("ERR-NORM-004", details={"code": code})
    return values[code]


def _round_money(amount: Decimal, rule: Mapping[str, Any]) -> Decimal:
    """ROUND_HALF_UP, not Python's default ROUND_HALF_EVEN: tz/06 says ≥0.5 goes
    up, and a banker's-rounding sum would be a cent off in a way nobody can
    explain to an accountant."""
    step = Decimal(str(rule.get("step", 1)))
    if rule.get("mode") == "half_up":
        return (amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step


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


def _apply_benefit(
    tariff: TariffFact, benefit_code: str | None
) -> tuple[Decimal, dict[str, Any] | None]:
    """The coefficient after a claimed benefit, and the breakdown line that
    records it — or `(tariff.coefficient, None)` unchanged when none is
    claimed. Ruling 20: a benefit code this tariff does not recognise is
    REJECTED, never silently ignored — charging full price for a claimed
    benefit is the worst of the three possible behaviours."""
    if benefit_code is None:
        return tariff.coefficient, None
    modifiers: dict[str, str] = tariff.benefit_modifiers or {}
    if benefit_code not in modifiers:
        raise err("ERR-VAL-001", details={"reason": "unknown_benefit_code", "code": benefit_code})
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


def max_sb(*, area_ha: Decimal, yield_c_per_ha: Decimal, params: Mapping[str, Any]) -> int:
    oz = yield_c_per_ha * area_ha * _decimal(params, "season_share")
    oz_eff = oz * _decimal(params, "safety_reserve")
    return int((oz_eff / _decimal(params, "sb_feed_norm")).to_integral_value(ROUND_FLOOR))


def calculate(request: CalcRequest, snapshot: ParamSnapshot) -> CalcResult:
    """Resolves the tariff (for grazing, one per livestock group via
    `tariff_group:<code>`; otherwise the single row with `livestock_group IS
    NULL`), applies a claimed benefit modifier, multiplies, sums, rounds once
    at the end, and computes `used_sb`/`max_sb`/`remaining_sb` when a norm is
    present. `breakdown` carries one line per tariff row applied, plus a
    `benefit` line per claimed-and-honoured benefit and a `limit` line when a
    norm is present — every `Decimal` already stringified by `jsonable`.
    `input_snapshot` is complete enough to recompute this exact row years from
    now without the database."""
    values = snapshot.values
    bhm = _decimal(values, "bhm")
    breakdown: list[dict[str, Any]] = []
    total = Decimal("0")

    if request.activity_code == GRAZING:
        for item in request.items:
            group = _param(values, f"tariff_group:{item.livestock_code}")
            tariff = _resolve_group_tariff(snapshot.tariffs, group)
            if tariff is None:
                breakdown.append(
                    {
                        "kind": "tariff",
                        "reason": "no_tariff_by_law",
                        "livestock_code": item.livestock_code,
                        "group": group,
                        "count": item.count,
                        "amount": jsonable(Decimal("0")),
                    }
                )
                continue
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
        if tariff is None:
            # Ruling 1: VMQ 278 has no rate for `science` — contracted
            # separately. A missing tariff is a legitimate zero, not an error.
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
    remaining_sb: Decimal | None = None
    if snapshot.norm is not None:
        used_sb = Decimal("0")
        for item in request.items:
            coefficient = _decimal(values, f"coef_sb:{item.livestock_code}")
            used_sb += coefficient * item.count
        max_sb_value = snapshot.norm.max_sb
        if max_sb_value is not None:
            remaining_sb = Decimal(max_sb_value) - snapshot.load_sb
        breakdown.append(
            {
                "kind": "limit",
                "used_sb": jsonable(used_sb),
                "max_sb": max_sb_value,
                "remaining_sb": jsonable(remaining_sb),
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
    }

    return CalcResult(
        amount=amount,
        used_sb=used_sb,
        max_sb=max_sb_value,
        remaining_sb=remaining_sb,
        breakdown=breakdown,
        rule_code_version=RULE_CODE_VERSION,
        input_snapshot=input_snapshot,
    )
