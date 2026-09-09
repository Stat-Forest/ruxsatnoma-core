"""Decision #178: a leshoz with no delivered GIS layer can still work — a
contour may exist without geometry at all, carrying its area as
`declared_area_ha` (copied onto `area_ha` on write) instead. Every check on
such a version reports `skipped`/`no_geometry`, never a silent `pass`; every
query that assumes geometry tolerates a NULL `geom` instead of erroring; and
`organizations.gis_enabled` is the switch that decides whether a leshoz shows
a map at all, independent of what any one row happens to carry."""

from decimal import Decimal

from sqlalchemy import func

from app.modules.gis import checks, repo
from tests.modules.gis.conftest import (
    box_wkt,
    make_contour,
    make_version,
    random_anchor,
    wkt_to_geojson,
)


async def test_creating_a_version_with_only_a_declared_area(gis_client, leshoz, contours_layer):
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "no-geom-1",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    version = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"source": "cadastre", "declared_area_ha": "2.5"},
    )
    assert version.status_code == 201, version.text
    body = version.json()
    assert body["status"] == "draft"
    assert body["area_ha"] == "2.5"  # copied from declared_area_ha, no geom to compute it from
    assert body["declared_area_ha"] == "2.5"


async def test_creating_a_version_with_neither_geometry_nor_declared_area_is_422(
    gis_client, leshoz, contours_layer
):
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "no-geom-2",
            "kind": "contour",
        },
    )
    contour_id = created.json()["id"]

    resp = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions", json={"source": "cadastre"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "ERR-VAL-001"


async def test_repo_insert_version_with_no_geometry_copies_declared_area_onto_area_ha(
    db, contours_layer, leshoz, gis_user
):
    """The repo-level path `create_version` drives, exercised directly — the
    same figure `norms` reads for pricing (`area_ha`, never `declared_area_ha`
    — module docstring) is what this proves lands correctly with no geometry
    to compute it from."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await repo.insert_version(
        db,
        contour_id=contour.id,
        version_no=1,
        source="cadastre",
        created_by=gis_user.id,
        declared_area_ha=Decimal("2.5000"),
    )
    assert version.geom is None
    assert version.area_ha == Decimal("2.5000")


async def test_gis_checks_report_skipped_no_geometry_for_every_check(
    db, geometryless_draft_version
):
    """Never a silent `pass`: nobody can assert a plot lies inside the fund,
    is valid, is clear of a restriction or an overlap with no geometry to
    test any of that against."""
    results = await checks.run_checks(db, version_id=geometryless_draft_version.id)
    assert {r["check"] for r in results} == {"validity", "within_fund", "restrictions", "overlap"}
    for result in results:
        assert result["result"] == "skipped"
        assert result["details"]["reason"] == "no_geometry"
    assert checks.is_blocked(results) is False


async def test_the_checks_endpoint_reports_skipped_for_a_geometryless_version(
    gis_client, geometryless_draft_version
):
    contour_id = geometryless_draft_version.contour_id
    version_id = geometryless_draft_version.id
    resp = await gis_client.post(f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/checks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["blocked"] is False
    assert all(
        c["result"] == "skipped" and c["details"]["reason"] == "no_geometry" for c in body["checks"]
    )


async def test_a_geometryless_contour_can_be_created_published_listed_and_priced(
    gis_client, rahbar_client, applicant_client, geometryless_draft_version, approval_doc, leshoz
):
    contour_id = geometryless_draft_version.contour_id
    version_id = geometryless_draft_version.id

    submitted = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/submit-review"
    )
    assert submitted.status_code == 200

    approved = await rahbar_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/approve",
        json={"approval_doc_id": str(approval_doc.id)},
    )
    assert approved.status_code == 200

    published = await rahbar_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/publish"
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    card = await applicant_client.get(f"/api/v1/gis/contours/{contour_id}")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["geometry"] is None
    assert body["area_ha"] == "2.5"  # the figure a real calculation would read
    assert body["occupied_ha"] == "0.0000"
    assert body["s_available_ha"] == "2.5"

    listed = await applicant_client.get(f"/api/v1/gis/contours?organization_id={leshoz.id}")
    assert listed.status_code == 200, listed.text
    items_by_id = {item["id"]: item for item in listed.json()["items"]}
    assert str(contour_id) in items_by_id
    assert items_by_id[str(contour_id)]["area_ha"] == "2.5"


async def test_patching_declared_area_syncs_area_ha_on_a_geometryless_draft(
    gis_client, geometryless_draft_version
):
    """`area_ha` IS the declared figure for a geometry-less draft — patching
    the latter without keeping the former in step would leave every
    downstream reader trusting a stale number."""
    cid = geometryless_draft_version.contour_id
    vid = geometryless_draft_version.id
    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{cid}/versions/{vid}", json={"declared_area_ha": "3.75"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["declared_area_ha"] == "3.75"
    assert body["area_ha"] == "3.75"


async def test_clearing_declared_area_on_a_geometryless_draft_is_422(
    gis_client, geometryless_draft_version
):
    """It is the ONLY area this version has — nulling it would leave `area_ha`
    with nothing to be, which the DB CHECK would otherwise turn into a 500."""
    cid = geometryless_draft_version.contour_id
    vid = geometryless_draft_version.id
    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{cid}/versions/{vid}", json={"declared_area_ha": None}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "declared_area_required_without_geometry"


async def test_splitting_a_geometryless_parent_is_refused(
    db, gis_client, published_contour_without_geometry
):
    """Distinct from `parent_not_published`: the version genuinely IS
    published, there is simply no shape on record for two pieces to
    reconstruct — and without this guard, `repo.split_partition_metrics`
    would read a NULL parent geometry and the service's own `assert` right
    after it would raise `AssertionError` (a 500), not a clean refusal."""
    parent_id = published_contour_without_geometry.contour_id
    payload = {
        "piece_a": {
            "number": "split-nogeo-a",
            "geom": await wkt_to_geojson(db, box_wkt(21.0, 21.0)),
        },
        "piece_b": {
            "number": "split-nogeo-b",
            "geom": await wkt_to_geojson(db, box_wkt(21.02, 21.0)),
        },
        "source": "survey",
    }
    resp = await gis_client.post(f"/api/v1/gis/contours/{parent_id}/split", json=payload)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-005"
    assert body["error"]["details"]["reason"] == "parent_has_no_geometry"


async def test_a_geometryless_published_contour_is_absent_from_the_map_but_not_the_list(
    applicant_client, leshoz, published_contour_without_geometry
):
    """`ST_AsGeoJSON(NULL)` is NULL, and `json.loads(None)` would raise — the
    map layer excludes such a contour rather than crashing on it, while the
    plain (no-geometry) list still shows it: that IS the requisites picker."""
    contour_id = published_contour_without_geometry.contour_id

    features = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}"
    )
    assert features.status_code == 200, features.text
    ids_on_map = {f["properties"]["contour_id"] for f in features.json()["features"]}
    assert str(contour_id) not in ids_on_map

    listed = await applicant_client.get(f"/api/v1/gis/contours?organization_id={leshoz.id}")
    assert str(contour_id) in {item["id"] for item in listed.json()["items"]}


async def test_a_leshoz_with_gis_enabled_off_shows_no_map(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """The switch is authoritative even over a row that DOES carry real
    geometry — `gis.service.contour_card`'s own reasoning: everything else
    (area, occupancy, the plain list) keeps working."""
    leshoz.gis_enabled = False
    await db.flush()
    lon, lat = random_anchor()
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        contour.id,
        box_wkt(lon, lat),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )

    features = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}"
    )
    assert features.status_code == 200, features.text
    assert features.json()["features"] == []

    card = await applicant_client.get(f"/api/v1/gis/contours/{contour.id}")
    assert card.status_code == 200, card.text
    assert card.json()["geometry"] is None
    assert card.json()["area_ha"] == "92"  # pricing data still there — only the map is hidden

    listed = await applicant_client.get(f"/api/v1/gis/contours?organization_id={leshoz.id}")
    assert str(contour.id) in {item["id"] for item in listed.json()["items"]}


async def test_a_contour_with_real_geometry_still_shows_its_map(
    applicant_client, leshoz, published_contour
):
    """The Burchmulla path, unchanged: `leshoz` defaults `gis_enabled=True`
    (decision #178), so the two new conditions on `contour_features_geojson`
    let a normal, geometry-bearing contour through exactly as before."""
    card = await applicant_client.get(f"/api/v1/gis/contours/{published_contour.contour_id}")
    assert card.status_code == 200, card.text
    assert card.json()["geometry"] is not None
    assert card.json()["geometry"]["type"] == "MultiPolygon"

    features = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}"
    )
    ids_on_map = {f["properties"]["contour_id"] for f in features.json()["features"]}
    assert str(published_contour.contour_id) in ids_on_map
