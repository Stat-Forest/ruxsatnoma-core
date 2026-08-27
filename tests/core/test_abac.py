"""ABAC zone filters compile to the right SQL conditions (no table needed)."""

import uuid

import pytest
from sqlalchemy import Column, Uuid, true

from app.core.abac import Zone, zone_filter, zone_of

region_col = Column("region_id", Uuid)
district_col = Column("district_id", Uuid)
organization_col = Column("organization_id", Uuid)


def test_republic_wide_zone_is_true():
    expr = zone_filter(Zone(None, None, None), region_col=region_col)
    assert str(expr) == str(true())


def test_region_zone_filters_region():
    rid = uuid.uuid4()
    expr = zone_filter(Zone(rid, None, None), region_col=region_col)
    assert "region_id =" in str(expr)


def test_district_zone_filters_both():
    rid, did = uuid.uuid4(), uuid.uuid4()
    expr = zone_filter(Zone(rid, did, None), region_col=region_col, district_col=district_col)
    s = str(expr)
    assert "region_id =" in s and "district_id =" in s


def test_region_zone_without_region_col_raises():
    rid = uuid.uuid4()
    with pytest.raises(ValueError, match="region"):
        zone_filter(Zone(rid, None, None))


def test_organization_zone_filters_organization():
    oid = uuid.uuid4()
    expr = zone_filter(Zone(None, None, oid), organization_col=organization_col)
    assert "organization_id =" in str(expr)


def test_zone_of_maps_object_attributes():
    class FakeUser:
        region_id = uuid.uuid4()
        district_id = None
        organization_id = uuid.uuid4()

    user = FakeUser()
    assert zone_of(user) == Zone(user.region_id, user.district_id, user.organization_id)
