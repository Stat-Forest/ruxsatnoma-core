"""Response shapes for `GET /gis/contours/{id}/occupancy`.

Field names deliberately mirror `norms.checks._capacity_result`'s own
`details` keys (`capacity`/`committed`/`remaining`/`unit`/`load_source`), the
shape T3 already renders in a refusal — an applicant reads the same word for
the same fact whether it comes from a check that blocked them or from the
calendar they consulted before filing. `Decimal` fields serialize through
plain `str()` (`norms.calculator.jsonable`'s own rule: never `float` for a
capacity/committed/remaining figure, and never `_trim_decimal`-style
stripping either — these are COMPUTED sums, not a value round-tripped
through one NUMERIC column, the same distinction `gis.schemas.
ContourCardOut.occupied_ha` draws for itself)."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

OccupancyLabel = Literal["free", "partial", "full"]


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class OccupancySubPeriodOut(BaseModel):
    """One stretch of the requested window with one committed figure. Sub-
    periods are contiguous and gapless: their `period_from`/`period_to` tile
    `[period_from, period_to]` of the parent response exactly.

    `committed`/`remaining` are `None` for an EXCLUSIVE contour (`result` is
    `free`/`full` only there) — there is no capacity number to state a
    remainder of, so a fabricated one is worse than none (`CLAUDE.md`:
    "loudly wrong beats silently wrong")."""

    model_config = ConfigDict(from_attributes=True)

    period_from: date
    period_to: date
    committed: Decimal | None
    remaining: Decimal | None
    result: OccupancyLabel

    @field_serializer("committed", "remaining")
    def _serialize_decimal(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class OccupancyOut(BaseModel):
    """`GET /gis/contours/{id}/occupancy`. No applicant identity anywhere in
    this shape — see `repo.active_permit_periods`'s own docstring for exactly
    which three columns of `permits` this is built from.

    `capacity`/`unit` are `None`/the activity's own unit respectively when
    `exclusive` is true (ruling #176, Oybek's option a): no norm, or the
    relevant column left unset, means the contour admits ONE active permit
    for this activity and refuses the rest, not "unlimited" — `unit` still
    names what a NON-exclusive answer would have been counted in, since it is
    a property of the activity, not of this one contour's capacity.

    `load_source` mirrors `norms.service.committed_load_sb`'s own idiom:
    `"permits"` means the committed figures below come from a real read of
    `permits`; `"none"` means they could not be resolved at all (today, only
    a non-grazing CAPACITY contour — see this track's report for why) and
    every sub-period's `committed` is a `0` placeholder, never a
    measurement."""

    model_config = ConfigDict(from_attributes=True)

    contour_id: uuid.UUID
    activity_type_id: uuid.UUID
    period_from: date
    period_to: date
    capacity: Decimal | None
    unit: str
    exclusive: bool
    load_source: Literal["permits", "none"]
    periods: list[OccupancySubPeriodOut]

    @field_serializer("capacity")
    def _serialize_capacity(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)
