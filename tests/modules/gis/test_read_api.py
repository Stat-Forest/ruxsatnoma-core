"""What 3.7 (norms) and 3.9 (applications) — and the applicant picking a plot —
actually read. S_available is a declared placeholder until permits land (ruling 14)."""

import pytest
from sqlalchemy import func

from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt


async def test_an_applicant_sees_published_contours_only(
    applicant_client, published_contour, draft_contour
):
    resp = await applicant_client.get("/api/v1/gis/contours")
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(published_contour.contour_id) in ids
    assert str(draft_contour.contour_id) not in ids


async def test_the_bbox_filter_excludes_what_is_outside_it(applicant_client, published_contour):
    inside = await applicant_client.get("/api/v1/gis/contours?bbox=69.8,41.4,70.0,41.6")
    outside = await applicant_client.get("/api/v1/gis/contours?bbox=60.0,41.4,60.1,41.5")
    assert len(inside.json()["items"]) >= 1
    assert outside.json()["items"] == []


async def test_a_malformed_bbox_is_422_not_500(applicant_client):
    resp = await applicant_client.get("/api/v1/gis/contours?bbox=nonsense")
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "bbox",
    [
        "nonsense",  # non-numeric
        "69.8,41.4,70.0",  # wrong count
        "70.0,41.6,69.8,41.4",  # min greater than max
        "nan,nan,nan,nan",  # float() accepts it and every NaN comparison is False
        "-inf,-inf,inf,inf",  # ...as does infinity, which passes min<max
        "-200,41.4,200,41.6",  # longitude outside +-180
        "69.8,-91,70.0,91",  # latitude outside +-90
    ],
)
async def test_every_bad_bbox_is_422_on_both_read_endpoints(applicant_client, bbox):
    """`nan`/`inf` parse as floats and `nan > nan` is False, so they used to
    pass both guards straight into `ST_MakeEnvelope`, where PostGIS raises —
    and `app/main.py` has no `DBAPIError` handler, so it surfaced as a 500 on
    two endpoints every authenticated user can reach. Out-of-range degrees
    were never checked at all."""
    contours = await applicant_client.get(f"/api/v1/gis/contours?bbox={bbox}")
    assert contours.status_code == 422, contours.text
    features = await applicant_client.get(f"/api/v1/gis/layers/fire_bans/features?bbox={bbox}")
    assert features.status_code == 422, features.text


async def test_the_list_is_filtered_for_a_region_scoped_actor(
    db, region_scoped_client, contours_layer, leshoz_in_fergana, other_leshoz, approval_doc
):
    """Review finding 2: `zone_filter` fails closed — it raises when a zone
    axis is set but its column was not supplied — and a region-scoped,
    organization-less actor (creatable today) hit exactly that. Must see a
    FILTERED list (the contour in their own region), not a 500 and not
    everything (a contour under an unrelated, region-less organization)."""
    in_region = await make_contour(db, contours_layer, leshoz_in_fergana)
    await make_version(
        db,
        in_region.id,
        random_box_wkt(),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    outside = await make_contour(db, contours_layer, other_leshoz)
    await make_version(
        db,
        outside.id,
        random_box_wkt(),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    resp = await region_scoped_client.get("/api/v1/gis/contours")
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(in_region.id) in ids
    assert str(outside.id) not in ids


async def test_the_contour_card_declares_that_occupancy_is_a_placeholder(
    applicant_client, published_contour
):
    """Ruling 14: no permits module yet — say so instead of implying a measurement."""
    resp = await applicant_client.get(f"/api/v1/gis/contours/{published_contour.contour_id}")
    body = resp.json()
    assert body["occupancy_source"] == "none"
    assert body["occupied_ha"] == "0.0000"
    assert body["s_available_ha"] == body["area_ha"]


async def test_a_registered_occupancy_provider_is_used(db, published_contour):
    """The seam 3.11 will fill: registering a provider changes the answer and the source."""
    from decimal import Decimal

    from app.modules.gis import service

    async def provider(db_, contour_id):
        return Decimal("10.0")

    service.OCCUPANCY_PROVIDERS.append(provider)
    try:
        occupied, source = await service.occupancy_ha(db, published_contour.contour_id)
        assert occupied == Decimal("10.0")
        assert source == "permits"
    finally:
        service.OCCUPANCY_PROVIDERS.remove(provider)


async def test_features_are_returned_as_a_geojson_feature_collection(
    gis_client, published_fire_ban
):
    resp = await gis_client.get("/api/v1/gis/layers/fire_bans/features?bbox=69.8,41.4,70.0,41.6")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert body["features"][0]["geometry"]["type"] in {"Polygon", "MultiPolygon"}


async def test_a_non_public_layer_is_refused_to_an_applicant(
    applicant_client, published_restriction
):
    resp = await applicant_client.get("/api/v1/gis/layers/restrictions/features")
    assert resp.status_code == 403
