"""The occupancy calendar (stage 9 wave 2, ruling #176/#177's three colours).
Read-only: builds, from ONE query against `permits` (`repo.
active_permit_periods`), the sub-periods a contour x activity's availability
splits into over a requested window — `free`, `partial` (remainder stated) or
`full`.

**Capacity resolution reuses `norms`'s own, rather than a second one.**
`norms.service.effective_norm` is the SAME published-norm lookup
`norms.checks`/`norms.params` resolve a request against (EXCLUDE-constrained,
one row in force per contour x activity x day); `norms.calculator.
resolve_capacity` is "the ONE place that picks between grazing's `max_sb` and
every other activity's own `capacity`" (its own docstring) — called here
exactly as `norms.params.load_snapshot` calls it. The unit mirrors
`norms.checks._capacity_unit`'s rule (grazing -> `"sb"`, every other activity
-> its own `quantity_unit`) rather than importing that private helper, since
`checks.py` exports nothing across the module boundary and is T7's file this
wave besides.

**Committed load.** Both columns the permit carries are read, chosen by
activity: `sb_load` for grazing (conditional heads) and `quantity` for every
other capacity activity (haymaking ha, apiary hives, deadwood m3, recreation
person-days), each swept from the ACTIVE permits' own date ranges.

Written when `permits` had `sb_load` and nothing else, this module reported
`committed=0`/`load_source="none"` for every non-grazing activity — honest
about the missing column, but it meant an occupied meadow drew GREEN on the
calendar, and green is the colour an applicant acts on. T6 added
`permits.quantity` in the same wave and the integration wired it here: the
placeholder branch is gone, and a track's honest "no data yet" survived
exactly as long as the data was genuinely missing.

**Exclusivity needs no quantity at all.** `period_from`/`period_to` exist on
every permit regardless of activity, so the EXCLUSIVE branch (no capacity at
all, ruling #176's option a) is exact for every activity, grazing included."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.time import business_today
from app.modules.admin import repo as admin_repo
from app.modules.gis import service as gis_service
from app.modules.norms import calculator
from app.modules.norms import service as norms_service
from app.modules.norms.models import Norm
from app.modules.occupancy import repo
from app.modules.occupancy.repo import PermitPeriod

_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class SubPeriod:
    period_from: date
    period_to: date
    committed: Decimal | None
    remaining: Decimal | None
    result: str


@dataclass(frozen=True)
class OccupancyResult:
    contour_id: uuid.UUID
    activity_type_id: uuid.UUID
    period_from: date
    period_to: date
    capacity: Decimal | None
    unit: str
    exclusive: bool
    load_source: str
    periods: list[SubPeriod]


def _to_norm_fact(norm: Norm | None) -> calculator.NormFact | None:
    """The five fields `calculator.resolve_capacity` reads, off the ORM row
    `norms.service.effective_norm` hands back — this module is a level-5-
    shaped reader (`repo.py`'s own docstring, design/01 rule 5), so importing
    `norms.models.Norm` for this one conversion is the same right
    `dashboard.repo` already exercises over five OTHER modules' models, not a
    new exception."""
    if norm is None:
        return None
    return calculator.NormFact(
        id=norm.id,
        yield_c_per_ha=norm.yield_c_per_ha,
        max_sb=norm.max_sb,
        season=norm.season,
        rotation=norm.rotation,
        capacity=norm.capacity,
    )


def _capacity_unit(activity_code: str, quantity_unit: str) -> str:
    """Mirrors `norms.checks._capacity_unit`'s rule exactly (grazing counts
    conditional heads, "sb", never the tariff's own unit; every other
    activity counts its own `activity_types.quantity_unit`) — duplicated
    rather than imported, since `checks.py` is a private module (T7's file
    this wave) that exports no public function for it."""
    return "sb" if activity_code == calculator.GRAZING else quantity_unit


def _merge_intervals(intervals: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Sorted, overlap- and touch-merged — two permits ending and starting on
    consecutive days occupy one unbroken stretch, not two with a zero-length
    gap between them."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + _ONE_DAY:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _exclusive_sub_periods(
    period_from: date, period_to: date, permits: list[PermitPeriod]
) -> list[SubPeriod]:
    """Ruling #176's exclusive case: `full` for every day an ACTIVE permit
    (for this contour x activity) covers, `free` elsewhere — built from the
    permits' own clipped date ranges, never a day-by-day walk."""
    intervals = [(max(p.period_from, period_from), min(p.period_to, period_to)) for p in permits]
    merged = _merge_intervals(intervals)
    sub_periods: list[SubPeriod] = []
    cursor = period_from
    for start, end in merged:
        if cursor < start:
            sub_periods.append(SubPeriod(cursor, start - _ONE_DAY, None, None, "free"))
        sub_periods.append(SubPeriod(start, end, None, None, "full"))
        cursor = end + _ONE_DAY
    if cursor <= period_to:
        sub_periods.append(SubPeriod(cursor, period_to, None, None, "free"))
    return sub_periods


def _label(start: date, end: date, committed: Decimal, capacity: Decimal) -> SubPeriod:
    remaining = capacity - committed
    if committed <= Decimal("0"):
        result = "free"
    elif committed >= capacity:
        result = "full"
    else:
        result = "partial"
    return SubPeriod(start, end, committed, remaining, result)


def _collapse(sub_periods: list[SubPeriod]) -> list[SubPeriod]:
    """Adjacent sub-periods with the identical committed figure are one
    stretch, not several — a boundary where nothing actually changed (two
    permits' events landing on the same day without moving the running total)
    must not fragment the calendar."""
    if not sub_periods:
        return []
    collapsed = [sub_periods[0]]
    for sub_period in sub_periods[1:]:
        last = collapsed[-1]
        contiguous = last.period_to + _ONE_DAY == sub_period.period_from
        if last.committed == sub_period.committed and contiguous:
            collapsed[-1] = SubPeriod(
                last.period_from,
                sub_period.period_to,
                last.committed,
                last.remaining,
                last.result,
            )
        else:
            collapsed.append(sub_period)
    return collapsed


def _boundaries(
    period_from: date, period_to: date, intervals: list[tuple[date, date]]
) -> list[date]:
    """Every date at which the committed total could change: each interval's
    own (clipped) start, and the day after its (clipped) end — a sweep over
    the permits' own dates, never over the calendar."""
    points = {period_from}
    for start, end in intervals:
        if period_from <= start <= period_to:
            points.add(start)
        after = end + _ONE_DAY
        if period_from <= after <= period_to:
            points.add(after)
    return sorted(points)


def _committed_of(permit: PermitPeriod, activity_code: str) -> Decimal:
    """What this permit committed, in the unit its activity counts in.

    Picked BY ACTIVITY rather than by coalescing the two columns: grazing
    counts conditional heads (`sb_load`) and everything else counts its own
    quantity, and a coalesce would read hectares as heads the day a row ever
    carried both. A null means the permit committed nothing measurable, which
    is zero — never a reason to skip the sub-period."""
    if activity_code == calculator.GRAZING:
        return permit.sb_load or Decimal("0")
    return permit.quantity or Decimal("0")


def _capacity_sub_periods(
    period_from: date,
    period_to: date,
    permits: list[PermitPeriod],
    capacity: Decimal,
    activity_code: str,
) -> list[SubPeriod]:
    """Every capacity activity: a sweep over the permits' own clipped date
    ranges, summing the concurrent committed load per stretch and labelling it
    against `capacity` — the finer-grained sibling of
    `norms.checks._capacity_result`'s own whole-window sum, exactly because a
    calendar's whole point is where the answer changes over time.

    Grazing-only when this was written: `permits` then carried `sb_load` and
    nothing else, so a haymaking permit committed a number this module could
    not see and the calendar painted an occupied meadow green. T6 added
    `permits.quantity` in the same wave; `_committed_of` reads whichever
    column the activity actually uses (integration, stage 9 wave 2)."""
    intervals = [
        (
            max(p.period_from, period_from),
            min(p.period_to, period_to),
            _committed_of(p, activity_code),
        )
        for p in permits
    ]
    if not intervals:
        return [_label(period_from, period_to, Decimal("0"), capacity)]
    boundaries = _boundaries(period_from, period_to, [(start, end) for start, end, _ in intervals])
    sub_periods: list[SubPeriod] = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] - _ONE_DAY if index + 1 < len(boundaries) else period_to
        committed = sum((load for s, e, load in intervals if s <= start <= e), Decimal("0"))
        sub_periods.append(_label(start, end, committed, capacity))
    return _collapse(sub_periods)


async def get_occupancy(
    db: AsyncSession,
    *,
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> OccupancyResult:
    """`GET /gis/contours/{id}/occupancy`. No permission and no zone rule —
    mirrors `gis.router.get_contour_card`/`list_contours` (ruling 5: reading a
    published contour needs neither, so an applicant can use this while
    picking a plot); the caller passes only `get_current_user`.

    **The capacity resolves ONE norm, at `business_today()`, for the WHOLE
    requested window** — not one norm per sub-period, even though a norm's
    own `effective_from`/`effective_to` could in principle change mid-window.
    This mirrors every existing admissibility check in `norms.checks`
    (`_norm_check`, `_season_check`, ...), all of which resolve a single norm
    at the request's own `on_date` for its whole period; a calendar asking
    "what does today's norm say about this future window" is the same
    question a citizen previewing a calculation already gets answered the
    same way. A norm due to change WITHIN the window is a real edge case
    neither this endpoint nor the check it mirrors handles today."""
    if period_to < period_from:
        raise err("ERR-VAL-001", details={"reason": "period_reversed"})
    if await gis_service.contour_organization(db, contour_id) is None:
        raise err("ERR-SYS-003", details={"contour": str(contour_id)})
    activity = await admin_repo.get_activity_type(db, activity_type_id)
    if activity is None:
        raise err("ERR-VAL-001", details={"reason": "unknown_activity_type"})

    norm = await norms_service.effective_norm(db, contour_id, activity_type_id, business_today())
    capacity = calculator.resolve_capacity(activity.code, _to_norm_fact(norm))
    unit = _capacity_unit(activity.code, activity.quantity_unit)

    permits = await repo.active_permit_periods(
        db, contour_id, activity_type_id, period_from, period_to
    )

    if capacity is None:
        periods = _exclusive_sub_periods(period_from, period_to, permits)
        load_source = "permits"
    else:
        periods = _capacity_sub_periods(period_from, period_to, permits, capacity, activity.code)
        load_source = "permits"

    return OccupancyResult(
        contour_id=contour_id,
        activity_type_id=activity_type_id,
        period_from=period_from,
        period_to=period_to,
        capacity=capacity,
        unit=unit,
        exclusive=capacity is None,
        load_source=load_source,
        periods=periods,
    )
