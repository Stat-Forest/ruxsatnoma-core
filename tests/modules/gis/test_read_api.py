"""What 3.7 (norms) and 3.9 (applications) — and the applicant picking a plot —
actually read. S_available is a declared placeholder until permits land (ruling 14)."""


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
