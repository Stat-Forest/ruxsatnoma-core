"""ABAC zone filters (design/01: 'own zone' mechanics). Level 0: takes columns, never models."""

import uuid
from typing import NamedTuple

from sqlalchemy import ColumnElement, and_, true


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
    region_col: ColumnElement | None = None,
    district_col: ColumnElement | None = None,
    organization_col: ColumnElement | None = None,
):
    """Boolean SQL expression limiting a query to the user's zone.

    All zone fields None → republic-wide: no restriction (true()).
    Region set → region must match; district additionally if both sides have it;
    organization likewise. Callers pass the columns their table actually has.
    """
    conditions = []
    if zone.region_id is not None and region_col is not None:
        conditions.append(region_col == zone.region_id)
    if zone.district_id is not None and district_col is not None:
        conditions.append(district_col == zone.district_id)
    if zone.organization_id is not None and organization_col is not None:
        conditions.append(organization_col == zone.organization_id)
    return and_(*conditions) if conditions else true()
