"""ABAC zone filters (design/01: 'own zone' mechanics). Level 0: takes columns, never models."""

import uuid
from typing import NamedTuple

from sqlalchemy import ColumnElement, ColumnExpressionArgument, and_, true


class Zone(NamedTuple):
    region_id: uuid.UUID | None
    district_id: uuid.UUID | None
    organization_id: uuid.UUID | None


def zone_of(user) -> Zone:
    """Extract the ABAC zone from any object with the three attributes (duck-typed:
    core cannot import auth models)."""
    return Zone(user.region_id, user.district_id, user.organization_id)


def zone_filter(
    zone: Zone,
    *,
    region_col: ColumnExpressionArgument | None = None,
    district_col: ColumnExpressionArgument | None = None,
    organization_col: ColumnExpressionArgument | None = None,
) -> ColumnElement[bool]:
    """Boolean SQL expression limiting a query to the user's zone.

    All zone fields None → republic-wide: no restriction (true()).
    Region set → region must match; district additionally if both sides have it;
    organization likewise. Callers pass the columns their table actually has.

    Fails closed: a zone field that IS set but whose column was not supplied is a
    caller bug (the table has no such column, so the restriction would silently
    not apply and leak rows outside the zone) — raise rather than let that pass.

    `ColumnExpressionArgument` (not the narrower `ColumnElement`) so a caller can
    pass a mapped attribute directly (e.g. `User.region_id`) — that is
    `InstrumentedAttribute`, not `ColumnElement`, but `col == value` below works on
    either identically; this is the same alias SQLAlchemy's own `.where()` uses.
    """
    conditions = []
    for value, col, field in (
        (zone.region_id, region_col, "region"),
        (zone.district_id, district_col, "district"),
        (zone.organization_id, organization_col, "organization"),
    ):
        if value is None:
            continue
        if col is None:
            raise ValueError(f"zone has {field} but no {field}_col was supplied")
        conditions.append(col == value)
    return and_(*conditions) if conditions else true()
