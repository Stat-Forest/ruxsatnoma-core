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
from app.modules.norms.calculator import GRAZING, CalcRequest, NormFact, ParamSnapshot, jsonable


class CheckResult(TypedDict):
    check: str
    result: str  # pass | fail | warning | skipped
    details: dict[str, Any]


BLOCKING = frozenset({"norm", "season", "rotation", "fire_ban", "limit"})

SEASON_ERROR = "ERR-NORM-003"

# VMQ 689's geobotanical survey — the basis of a norm's own `season`/
# `rotation` — is redone every five years, so a requested period cannot
# outlive the survey it is checked against; this is a domain ceiling, not a
# tuning knob for the day-by-day walk below (though it also keeps that walk
# to a few thousand iterations at most, never tens of thousands).
MAX_PERIOD_DAYS = 5 * 365

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


def _in_window(day: date, window: Mapping[str, str]) -> bool:
    """A window is a recurring MM-DD range (ruling 14). A window whose `from` is
    later than its `to` wraps the new year — winter pasture — and a day is inside
    it when it is after `from` OR before `to`, not both."""
    start, end = window["from"], window["to"]
    stamp = day.strftime("%m-%d")
    return start <= stamp <= end if start <= end else stamp >= start or stamp <= end


def _season_check(
    period_from: date, period_to: date, season: Mapping[str, Any] | None
) -> CheckResult:
    """Walks every day of the requested period, not just its two ends: two
    windows can each cover one end of a period while leaving a gap between them
    uncovered (e.g. windows 04-01..05-31 and 07-01..08-31 around a request
    running 04-15..08-15) — checking only `period_from`/`period_to` would find
    both endpoints inside A WINDOW and wrongly pass, missing June entirely.
    Periods here are seasonal (months, not decades), so a plain day-by-day walk
    is both correct and cheap — no need to reason about which window edges
    could matter.

    No `windows` configured is not the same as "always in season" — it means
    this norm never recorded one, so the check reports `skipped` rather than
    asserting something nobody verified."""
    if not season or not season.get("windows"):
        return {"check": "season", "result": "skipped", "details": {"reason": "no_season_defined"}}
    windows = season["windows"]
    day = period_from
    while day <= period_to:
        if not any(_in_window(day, window) for window in windows):
            return {"check": "season", "result": "fail", "details": {"reason": "outside_season"}}
        day += timedelta(days=1)
    return {"check": "season", "result": "pass", "details": {}}


def _rotation_check(
    period_from: date, period_to: date, rotation: Mapping[str, Any] | None
) -> CheckResult:
    """Every calendar year the period touches, against `rest_years` — a period
    crossing a year boundary must fail if EITHER year it touches is resting. No
    `rotation` configured means no rest years at all, which is a confident
    `pass` (unlike an unconfigured season, absence here is a definite fact, not
    an unanswered question)."""
    rest_years = (rotation or {}).get("rest_years", [])
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
    did not."""
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


def _limit_check(snapshot: ParamSnapshot, used_sb: Decimal | None) -> CheckResult:
    """Open question #1 from Task 5's review: nothing compared `used_sb`
    against `remaining_sb` until this check exists — `remaining_sb` here is
    computed the SAME way `calculator.calculate` computes it (`max_sb −
    committed load`, excluding the request's own load), so a caller cannot see
    two different numbers for the same name.

    `used_sb=None` is the reviewer-facing path — checks without the money —
    and is reported `skipped`, never a manufactured zero load. A norm with no
    `max_sb` (never frozen, or no norm at all) has nothing to compare against
    either, and is `skipped` the same way."""
    if used_sb is None:
        return {"check": "limit", "result": "skipped", "details": {"reason": "not_computed"}}
    norm = snapshot.norm
    if norm is None or norm.max_sb is None:
        return {"check": "limit", "result": "skipped", "details": {"reason": "no_limit"}}
    committed_sb = snapshot.load_sb
    remaining_sb = Decimal(norm.max_sb) - committed_sb
    details = {
        "used_sb": jsonable(used_sb),
        "max_sb": norm.max_sb,
        "committed_sb": jsonable(committed_sb),
        "remaining_sb": jsonable(remaining_sb),
        "load_source": snapshot.load_source,
    }
    result = "fail" if used_sb > remaining_sb else "pass"
    return {"check": "limit", "result": result, "details": details}


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

    results: list[CheckResult] = [_norm_check(request, snapshot.norm)]

    if snapshot.norm is not None:
        results.append(_season_check(period_from, period_to, snapshot.norm.season))
        results.append(_rotation_check(period_from, period_to, snapshot.norm.rotation))
    else:
        results.append({"check": "season", "result": "skipped", "details": {"reason": "no_norm"}})
        results.append({"check": "rotation", "result": "skipped", "details": {"reason": "no_norm"}})

    fire_ban, restrictions = await _territory_checks(db, contour_id, period_from, period_to)
    results.append(fire_ban)
    results.append(restrictions)

    results.append(_limit_check(snapshot, used_sb))

    return results
