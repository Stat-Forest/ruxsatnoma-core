"""ABAC zone filters compile to the right SQL conditions (no table needed)."""

import uuid

from sqlalchemy import Column, Uuid, true

from app.core.abac import Zone, zone_filter

region_col = Column("region_id", Uuid)
district_col = Column("district_id", Uuid)


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
