"""Season, rotation, fire bans, restrictions and the limit — the rules that decide
whether a request is admissible at all, as opposed to how much it costs.

The result shape is deliberately identical to `gis.checks.CheckResult`: a
front-end shows one list of checks, and 3.9 stores one list on the application.
BLOCKING says which of them refuse; everything else is advice for the reviewer."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, err
from app.modules.gis import service as gis_service
from app.modules.norms import calculator, repo
from app.modules.norms.calculator import (
    GRAZING,
    CalcRequest,
    NormFact,
    ParamSnapshot,
    jsonable,
)


class CheckResult(TypedDict):
    check: str
    result: str  # pass | fail | warning | skipped
    details: dict[str, Any]


BLOCKING = frozenset({"norm", "season", "min_term", "rotation", "fire_ban", "limit"})

SEASON_ERROR = "ERR-NORM-003"

# VMQ 689's geobotanical survey — the basis of a norm's own `season`/
# `rotation` — is redone every five years, so a requested period cannot
# outlive the survey it is checked against; this is a domain ceiling, not a
# tuning knob for the day-by-day walk below (though it also keeps that walk
# to a few thousand iterations at most, never tens of thousands).
#
# 366, not 365 (I10): a real five-CALENDAR-year span is 1826-1827 days
# (`2024-01-01 .. 2028-12-31` is `.days == 1826`), so `5 * 365` refused a
# lawful maximum period by a day or two.
MAX_PERIOD_DAYS = 5 * 366

# Ruling 15: one round trip against all three layers, split by code afterwards
# — `fire_bans` becomes its own blocking check, `restrictions`/`protection`
# fold into one advisory check. Re-declared here (not imported from
# `gis.models.RESTRICTION_LAYER_CODES`): `norms` reaches `gis` only through
# `gis.service`, never `gis.models`/`gis.repo` (module boundary rule).
_TERRITORY_LAYER_CODES = ("fire_bans", "restrictions", "protection")

_ERROR_BY_CHECK = {
    "norm": "ERR-NORM-001",
    "limit": "ERR-NORM-002",
    "season": SEASON_ERROR,
    "min_term": SEASON_ERROR,
    "rotation": SEASON_ERROR,
    "fire_ban": SEASON_ERROR,
}


def is_blocked(results: list[CheckResult]) -> bool:
    return any(r["check"] in BLOCKING and r["result"] == "fail" for r in results)


def first_blocking_error(results: list[CheckResult]) -> DomainError | None:
    """The first BLOCKING failure (in the order `run_checks` produced them),
    mapped onto its ERR-NORM code. `details` carries the WHOLE check list, not
    just the one that blocked, so a caller never has to re-run anything to see
    why — coerced through `calculator.jsonable` first (lesson: `DomainError`'s
    JSON response has no encoder of its own, and several checks below carry a
    raw `Decimal`/`uuid.UUID` straight from a `gis.service.features_intersecting`
    row)."""
    for result in results:
        if result["check"] in BLOCKING and result["result"] == "fail":
            return err(_ERROR_BY_CHECK[result["check"]], details={"checks": jsonable(results)})
    return None


def _in_window(day: date, window: Any) -> bool:
    """A window is a recurring MM-DD range (ruling 14). A window whose `from` is
    later than its `to` wraps the new year — winter pasture — and a day is inside
    it when it is after `from` OR before `to`, not both.

    Reads defensively, and fails CLOSED (I5, final review). `schemas.Season`
    now refuses a malformed window at the edge, but `norms.season` is a JSONB
    column that has been free-form until today, so a row written before that
    validation existed can still hold anything. This used to be
    `window["from"]` — a `KeyError` raised INSIDE a check, i.e. an uncaught
    500 on `POST /calculations/preview` rather than a domain answer, and a
    `TypeError` the same way for a non-string bound. A window that cannot be
    read simply does not cover the day, so an unreadable season blocks
    instead of passing."""
    if not isinstance(window, Mapping):
        return False
    start, end = window.get("from"), window.get("to")
    if not isinstance(start, str) or not isinstance(end, str):
        return False
    stamp = day.strftime("%m-%d")
    return start <= stamp <= end if start <= end else stamp >= start or stamp <= end


def resolve_effective_windows(
    norm_season: Mapping[str, Any] | None, dictionary_season: Mapping[str, Any] | None
) -> tuple[list[Any], str]:
    """Ruling #177's override order, and the ONE place it is decided — both
    `_season_check` below and `service.effective_season` (task 4's public
    read for the wizard) call this rather than each re-deriving the
    precedence, so a date picker built from the public read can never
    disagree with the check that fires if the applicant ignores it (lesson:
    a precondition/decision shared by several callers belongs in one
    function every one of them calls).

    The contour's own norm wins when it states windows of its own; the
    leshoz dictionary (`activity_seasons`, keyed by organization × activity)
    is the fallback a leshoz states once instead of on every one of its
    contours; neither present is `("none", [])` — today's exact meaning,
    unchanged: `no_season_defined`. Reads both defensively (`isinstance`
    before `.get`), the same fail-closed posture `_in_window` already uses —
    a malformed `season`/`windows` value never crashes this function, it
    simply does not count as "has windows"."""
    if isinstance(norm_season, Mapping) and norm_season.get("windows"):
        return list(norm_season["windows"]), "norm"
    if isinstance(dictionary_season, Mapping) and dictionary_season.get("windows"):
        return list(dictionary_season["windows"]), "activity_season"
    return [], "none"


def _season_check(
    period_from: date,
    period_to: date,
    norm_season: Mapping[str, Any] | None,
    dictionary_season: Mapping[str, Any] | None,
) -> CheckResult:
    """Walks every day of the requested period, not just its two ends: two
    windows can each cover one end of a period while leaving a gap between them
    uncovered (e.g. windows 04-01..05-31 and 07-01..08-31 around a request
    running 04-15..08-15) — checking only `period_from`/`period_to` would find
    both endpoints inside A WINDOW and wrongly pass, missing June entirely.
    Periods here are seasonal (months, not decades), so a plain day-by-day walk
    is both correct and cheap — no need to reason about which window edges
    could matter.

    No windows resolved at all (`resolve_effective_windows` — neither the
    contour's own norm nor the leshoz dictionary states any) is not the same
    as "always in season" — it means nobody has recorded one anywhere, so the
    check reports `skipped` rather than asserting something nobody verified.
    `details.source` says which of the two won when one did (ruling #177),
    so a caller can tell a norm override from the leshoz default without a
    second round trip."""
    windows, source = resolve_effective_windows(norm_season, dictionary_season)
    if not windows:
        return {"check": "season", "result": "skipped", "details": {"reason": "no_season_defined"}}
    day = period_from
    while day <= period_to:
        if not any(_in_window(day, window) for window in windows):
            return {
                "check": "season",
                "result": "fail",
                "details": {"reason": "outside_season", "source": source},
            }
        day += timedelta(days=1)
    return {"check": "season", "result": "pass", "details": {"source": source}}


def _min_term_check(period_from: date, period_to: date, min_term_days: int | None) -> CheckResult:
    """Ruling #177 task 3: the leshoz dictionary's own `min_term_days`,
    blocking, `details.min_term_days` carried on every outcome (not just the
    failure) so the UI can STATE the rule rather than merely enforce it.

    No norm-level override exists for this figure — the ruling only speaks
    of overriding the WINDOWS — so it reads `activity_seasons` alone; no
    dictionary row, or the column left NULL, means no minimum is enforced
    (`skipped`), never a manufactured zero. `requested_days` is inclusive
    of both ends (`payments.refunds`'s own `total_days` convention for "how
    many days is this period"), so a one-day request against a one-day
    minimum passes rather than failing by construction."""
    if min_term_days is None:
        return {
            "check": "min_term",
            "result": "skipped",
            "details": {"reason": "no_min_term_defined"},
        }
    requested_days = (period_to - period_from).days + 1
    if requested_days < min_term_days:
        return {
            "check": "min_term",
            "result": "fail",
            "details": {
                "reason": "period_too_short",
                "min_term_days": min_term_days,
                "requested_days": requested_days,
            },
        }
    return {"check": "min_term", "result": "pass", "details": {"min_term_days": min_term_days}}


def _rotation_check(
    period_from: date, period_to: date, rotation: Mapping[str, Any] | None
) -> CheckResult:
    """Every calendar year the period touches, against `rest_years` — a period
    crossing a year boundary must fail if EITHER year it touches is resting. No
    `rotation` configured means no rest years at all, which is a confident
    `pass` (unlike an unconfigured season, absence here is a definite fact, not
    an unanswered question)."""
    raw_rest_years = (rotation or {}).get("rest_years") or []
    # Coerced, not trusted (I5, final review). `schemas.Rotation` now types
    # these as integers, but a row written before that validation existed can
    # hold `{"rest_years": ["2027"]}` — the shape a JSON form happily produces
    # — and `if year in rest_years` compared an `int` against a `str`, passing
    # SILENTLY for a resting year: a fail-open on a blocking check. Anything
    # that will not convert is dropped rather than crashing a check.
    rest_years = {int(y) for y in raw_rest_years if str(y).lstrip("-").isdigit()}
    for year in range(period_from.year, period_to.year + 1):
        if year in rest_years:
            return {
                "check": "rotation",
                "result": "fail",
                "details": {"reason": "rest_year", "year": year},
            }
    return {"check": "rotation", "result": "pass", "details": {}}


def _norm_check(request: CalcRequest, norm: NormFact | None) -> CheckResult:
    """Ruling 13: VMQ 689 imposes a feed-stock limit on grazing, and on nothing
    else — a missing norm is `ERR-NORM-001` for grazing, and a normal, silent
    `skipped` for every other activity."""
    if request.activity_code != GRAZING:
        return {
            "check": "norm",
            "result": "skipped",
            "details": {"reason": "not_required_for_activity"},
        }
    if norm is None:
        return {"check": "norm", "result": "fail", "details": {"reason": "no_approved_norm"}}
    return {"check": "norm", "result": "pass", "details": {}}


def _feature_items(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """The same item shape `gis.checks._intersections` returns (`layer`,
    `feature_id`, `name`, `area_m2`) — one shape a front-end already knows how
    to render, whichever module produced the list."""
    return [
        {"layer": row.layer_code, "feature_id": row.id, "name": row.name, "area_m2": row.area_m2}
        for row in rows
    ]


async def _territory_checks(
    db: AsyncSession, contour_id: uuid.UUID, period_from: date, period_to: date
) -> tuple[CheckResult, CheckResult]:
    """One round trip for both: a `fire_bans` intersection blocks (ruling 15,
    the split 3.6a deliberately left open — `gis.checks` never fails on any of
    these three layers); `restrictions`/`protection` only ever warn, exactly as
    they did in 3.6a — the DECISION moved here, the layers' own advisory status
    did not.

    Both report `skipped` when the contour has no PUBLISHED geometry (I6,
    final review). `gis.repo.features_intersecting` inner-joins that version,
    so a contour whose geometry is still draft returns zero features — and a
    zero result used to read as `pass`, a BLOCKING safety check asserting a
    fact it never tested. The state is reachable:
    `service._build_request_and_snapshot` deliberately tolerates a contour
    with no published version (`area_ha = 0`), and a non-grazing activity
    needs no norm either, so nothing else demands one. This is exactly the
    anti-pattern `_season_check`'s own docstring articulates — "no `windows`
    configured is not the same as 'always in season'" — applied consistently:
    nothing verified, nothing asserted."""
    if await gis_service.published_version(db, contour_id) is None:
        no_geometry: dict[str, Any] = {"reason": "no_published_geometry"}
        return (
            {"check": "fire_ban", "result": "skipped", "details": no_geometry},
            {"check": "restrictions", "result": "skipped", "details": no_geometry},
        )
    features = await gis_service.features_intersecting(
        db, contour_id, _TERRITORY_LAYER_CODES, period_from, period_to
    )
    fire_bans = [row for row in features if row.layer_code == "fire_bans"]
    restrictions = [row for row in features if row.layer_code != "fire_bans"]

    fire_ban: CheckResult
    if fire_bans:
        fire_ban = {
            "check": "fire_ban",
            "result": "fail",
            "details": {"reason": "fire_ban", "items": _feature_items(fire_bans)},
        }
    else:
        fire_ban = {"check": "fire_ban", "result": "pass", "details": {}}

    restriction_result: CheckResult
    if restrictions:
        restriction_result = {
            "check": "restrictions",
            "result": "warning",
            "details": {"items": _feature_items(restrictions)},
        }
    else:
        restriction_result = {"check": "restrictions", "result": "pass", "details": {}}

    return fire_ban, restriction_result


def _resolve_requested(request: CalcRequest, used_sb: Decimal | None) -> Decimal | None:
    """What is being asked for, in the capacity's own unit. Grazing compares
    conditional heads (`used_sb`) — a PRICED fact `calculator.calculate`
    derives from `coef_sb:<code>` and hands in, never re-derived here, the
    same "checks without the money" boundary this module's own reviewer path
    already drew (`run_checks`'s `used_sb=None` caller). Every other activity
    compares the plain declared `request.quantity` instead: VMQ 278's
    quantity needs no pricing step to be known, so it is available even on
    that unpriced path — which is why a haymaking/apiary/deadwood/recreation
    admissibility screen can now show a real capacity comparison where
    grazing's own screen still shows `skipped`."""
    if request.activity_code == GRAZING:
        return used_sb
    return request.quantity


def _capacity_unit(request: CalcRequest, snapshot: ParamSnapshot) -> str | None:
    """The unit the three capacity numbers are counted in — the one thing
    `requested`/`capacity`/`remaining` cannot be read without.

    Integration finding, stage 9 wave 1: T4 generalised the check across every
    activity and T3 rendered it, but nothing carried the unit, so a refusal
    said «40 of 100» with no way to know whether that meant hectares, hives or
    cubic metres. Grazing answers `"sb"` (условная голова) because its numbers
    are conditional heads rather than the tariff's own billing unit; every
    other activity answers its own `activity_types.quantity_unit`, carried on
    the snapshot. It is deliberately NOT read off the tariff rows beside it:
    `science` has no tariff row by law (`tz/06`) and still measures something.
    A snapshot built without a unit — every construction predating this stage,
    tests included — answers `None`, and the refusal then states no unit
    instead of inventing one.
    """
    if request.activity_code == GRAZING:
        return "sb"
    return snapshot.quantity_unit


def _capacity_result(
    request: CalcRequest,
    snapshot: ParamSnapshot,
    capacity: Decimal,
    used_sb: Decimal | None,
) -> CheckResult:
    """Ruling #176: `requested ≤ capacity − committed` — the same shape for
    every activity, differing only in which committed-load seam and which
    rounding rule apply. Open question #1 from Task 5's original review:
    nothing compared a request against its remainder until this check
    existed for grazing; the three keys below (`requested`/`capacity`/
    `remaining`) are the generalised, activity-agnostic names the front-end
    renders — never the grazing-only `used_sb`/`max_sb`/`remaining_sb` this
    check used to answer with."""
    requested = _resolve_requested(request, used_sb)
    if requested is None:
        # The reviewer-facing path for grazing (`used_sb=None`) — checks
        # without the money — never a manufactured zero demand.
        return {"check": "limit", "result": "skipped", "details": {"reason": "not_computed"}}
    if request.activity_code == GRAZING:
        # Unchanged math: `calculator.remaining_sb` floors by `rounding_heads`
        # (ruling 19 — a limit is never rounded in the applicant's favour),
        # the SAME number `calculator.calculate` stores, so the two can never
        # drift apart (Task 5/I1's own reasoning, preserved).
        committed = snapshot.load_sb
        remaining = calculator.remaining_sb(int(capacity), committed, snapshot.values)
        load_source = snapshot.load_source
    else:
        # A continuous unit (ha, m3, person_day, hive) — no integer floor to
        # apply; the raw Decimal comparison is the whole rule.
        committed = snapshot.capacity_load
        remaining = capacity - committed
        load_source = snapshot.capacity_load_source
    details = {
        "requested": jsonable(requested),
        "capacity": jsonable(capacity),
        "committed": jsonable(committed),
        "remaining": jsonable(remaining),
        "load_source": load_source,
        "unit": _capacity_unit(request, snapshot),
    }
    result = "fail" if requested > remaining else "pass"
    return {"check": "limit", "result": result, "details": details}


def _exclusivity_result(snapshot: ParamSnapshot) -> CheckResult:
    """Ruling #176, Oybek's option а: NO capacity at all — no norm, or the
    relevant column left unset — is EXCLUSIVE for the period, never
    unlimited. This deliberately changes grazing too: a norm with a null
    `max_sb` used to report `skipped`/`no_limit` here; it now lands in this
    same exclusive branch as every other capacity-less activity, because
    absence of a number was never permission to double-book.

    `occupied_until_source` tells apart the two HONEST outcomes from a third
    one this never reports: `"none"` means `EXCLUSIVITY_PROVIDERS` has
    nothing registered yet and the question could not be asked at all
    (`skipped`, never a manufactured "free"); `"permits"` means it WAS asked,
    and answers either the day the contour frees up or that nothing
    overlaps."""
    if snapshot.occupied_until_source == "none":
        return {
            "check": "limit",
            "result": "skipped",
            "details": {"reason": "no_occupancy_provider"},
        }
    if snapshot.occupied_until is not None:
        return {
            "check": "limit",
            "result": "fail",
            "details": {
                "reason": "exclusive_occupied",
                "occupied_until": jsonable(snapshot.occupied_until),
            },
        }
    return {"check": "limit", "result": "pass", "details": {"reason": "exclusive_available"}}


def _limit_check(
    request: CalcRequest, snapshot: ParamSnapshot, used_sb: Decimal | None
) -> CheckResult:
    """Ruling #176 (stage 9): the one general limit check, replacing the
    grazing-only pair this function used to be. Resolves the capacity for
    THIS activity (`calculator.resolve_capacity` — grazing's `max_sb`,
    everything else's own `capacity`) and, when one exists, refuses a
    request that would exceed what remains after the committed load for an
    OVERLAPPING period (`_capacity_result`); when none exists at all, the
    contour is EXCLUSIVE for the period, not unlimited (`_exclusivity_result`).
    `ERR-NORM-002`, BLOCKING either way — `checks.BLOCKING`/`_ERROR_BY_CHECK`
    are unchanged, only what fills `details` for the same `"limit"` check
    name differs by branch."""
    capacity = calculator.resolve_capacity(request.activity_code, snapshot.norm)
    if capacity is None:
        return _exclusivity_result(snapshot)
    return _capacity_result(request, snapshot, capacity, used_sb)


async def run_checks(
    db: AsyncSession,
    *,
    request: CalcRequest,
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    snapshot: ParamSnapshot,
    used_sb: Decimal | None = None,
) -> list[CheckResult]:
    """Every admissibility rule for one request, in one list. `is_blocked`
    says whether ANY of them refuses it; `first_blocking_error` says which
    ERR-NORM code to raise.

    `activity_type_id` is accepted for signature symmetry with
    `params.load_snapshot` (the same three pieces of request context) — every
    check below reads what it needs from `request`, `snapshot` and
    `contour_id` instead, since `snapshot.norm` was already resolved for this
    exact activity by the time it gets here.

    Guards `period_from`/`period_to` before any check runs, fail-closed: a
    reversed period would make `_season_check`'s walk and `_rotation_check`'s
    range both no-op to a false `pass`, and would make
    `features_intersecting`'s validity predicate (written assuming the normal
    ordering) drop a fire ban that genuinely covers the request out of its
    result set entirely — turning the one check this stage exists to make
    blocking into a false `pass` instead. A per-endpoint guard on whichever
    router eventually builds `CalcRequest` would not be a root fix: this
    module is the shared entry point (3.9 calls it directly too), so the
    guard lives here (lesson: a per-endpoint guard is not a root fix when
    sibling callers share a precondition)."""
    period_from, period_to = request.period_from, request.period_to
    if period_to < period_from:
        raise err("ERR-VAL-001", details={"reason": "period_reversed"})
    if (period_to - period_from).days > MAX_PERIOD_DAYS:
        raise err("ERR-VAL-001", details={"reason": "period_too_long"})

    # Ruling #177: the leshoz dictionary (`activity_seasons`) is resolved
    # once per contour × activity, regardless of whether a norm exists at
    # all — the whole point of the ruling is a season stated even where no
    # geobotanical survey, and therefore no `Norm`, exists yet.
    # `organization_id` is None only when the contour itself cannot be
    # resolved, which `service._build_request_and_snapshot` already refuses
    # before this function is ever reached — kept defensive here since this
    # module is the shared entry point 3.9 also calls directly.
    organization_id = await gis_service.contour_organization(db, contour_id)
    dictionary_row = (
        await repo.get_activity_season(db, organization_id, activity_type_id)
        if organization_id is not None
        else None
    )
    dictionary_season = dictionary_row.season if dictionary_row is not None else None
    dictionary_min_term = dictionary_row.min_term_days if dictionary_row is not None else None

    results: list[CheckResult] = [_norm_check(request, snapshot.norm)]

    norm_season = snapshot.norm.season if snapshot.norm is not None else None
    results.append(_season_check(period_from, period_to, norm_season, dictionary_season))
    results.append(_min_term_check(period_from, period_to, dictionary_min_term))

    if snapshot.norm is not None:
        results.append(_rotation_check(period_from, period_to, snapshot.norm.rotation))
    else:
        results.append({"check": "rotation", "result": "skipped", "details": {"reason": "no_norm"}})

    fire_ban, restrictions = await _territory_checks(db, contour_id, period_from, period_to)
    results.append(fire_ban)
    results.append(restrictions)

    results.append(_limit_check(request, snapshot, used_sb))

    return results
