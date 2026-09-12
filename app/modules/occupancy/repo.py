"""Occupancy repository. A level-5-shaped reader (design/01 rule 5: "reports,
dashboard, search, oversight and archive... build queries across the whole
database — they are allowed direct read-only access to any table"). This
module has no table of its own, only a calendar view built over `permits`.

**Why this reads `permits.models.Permit` directly rather than going through
`permits.service`.** `permits.service.occupancy_provider`/`load_provider` and
`norms.service`'s own provider seams (`committed_load_sb`/
`committed_capacity_load`/`occupied_until`) all answer ONE aggregate number
for the WHOLE window asked — that is what a blocking check needs, but a
calendar needs each ACTIVE permit's OWN `(period_from, period_to)` to know
where the label changes. No public read anywhere in `permits.service` (or
`norms.service`'s seams) exposes that, so this module reads the table
directly, the same way `dashboard.repo` already does (`from
app.modules.permits.models import Permit`, unchanged import in that file) —
see this stage's track report for the precedent and the gap this closes.

**Exactly three columns leave this query.** `period_from`, `period_to`,
`sb_load` — never `applicant_id`, never `snapshot` (which carries the
holder's name and PINFL, `permits.service._snapshot`), never
`organization_id`. `sb_load` is a conditional-head COUNT frozen at issuance,
not a fact about who holds the permit — see `test_no_personal_data.py` for
the assertion this shape is meant to satisfy."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permits.models import Permit

# Mirrors `permits.service.OCCUPYING_STATUSES` — repeated as literals rather
# than imported, because importing `permits.service` here would pull in that
# module's whole write surface for two strings; a reader takes the TABLE,
# never the service (module boundary, `CLAUDE.md`). A test pins the two
# together. `pending_signatures` is counted on purpose: an issued, paid-for
# permit awaiting its signatures reserves the slot, and a calendar painting
# those days green would show an applicant a slot every gate refuses.
OCCUPYING_STATUSES = ("pending_signatures", "active")


@dataclass(frozen=True)
class PermitPeriod:
    """One occupying permit's own dates and committed load — nothing that
    could identify who holds it.

    TWO load columns, because the permit carries two: `sb_load` is grazing's
    conditional-head count, `quantity` is every other activity's committed
    amount in its own unit (`permits.models.Permit`). Each is null wherever
    the other applies; `service._committed_of` picks by activity rather than
    coalescing them, since a coalesce would silently read hectares as heads
    the day a permit ever carried both."""

    period_from: date
    period_to: date
    sb_load: Decimal | None
    quantity: Decimal | None


async def active_permit_periods(
    db: AsyncSession,
    contour_id: uuid.UUID,
    activity_type_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> list[PermitPeriod]:
    """Every OCCUPYING permit's own period for this contour x activity,
    overlapping `[period_from, period_to]` — ONE query for the whole window,
    never one per day and never one per sub-period; `occupancy.service` builds
    every sub-period boundary from what this returns.

    The overlap predicate mirrors `permits.repo.committed_sb_load`'s own:
    both ends inclusive, `period_to` is the last day of use, not the day
    after. A reversed argument pair inverts it and hides the rows it should
    find (lesson) — `occupancy.service.get_occupancy` guards that fail-closed
    before this is ever called."""
    rows = await db.execute(
        select(Permit.period_from, Permit.period_to, Permit.sb_load, Permit.quantity).where(
            Permit.contour_id == contour_id,
            Permit.activity_type_id == activity_type_id,
            Permit.status.in_(OCCUPYING_STATUSES),
            Permit.period_from <= period_to,
            Permit.period_to >= period_from,
        )
    )
    return [
        PermitPeriod(period_from=row[0], period_to=row[1], sb_load=row[2], quantity=row[3])
        for row in rows.all()
    ]
