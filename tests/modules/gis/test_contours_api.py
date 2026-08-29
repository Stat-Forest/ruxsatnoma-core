"""Contour identity + draft versions through the API, with the zone rule of ruling 18."""


async def test_gis_specialist_creates_a_contour_and_a_draft_version(
    gis_client, leshoz, contours_layer
):
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "14515q",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    version = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={
            "geom": {
                "type": "Polygon",
                "coordinates": [
                    [[69.9, 41.5], [69.91, 41.5], [69.91, 41.51], [69.9, 41.51], [69.9, 41.5]]
                ],
            },
            "source": "survey",
            "declared_area_ha": "2.6",
            "accuracy_m": "1.5",
        },
    )
    assert version.status_code == 201
    body = version.json()
    assert body["status"] == "draft"
    assert body["version_no"] == 1
    assert float(body["area_ha"]) > 85  # computed, not the declared 2.6 (ruling 2)
    assert body["declared_area_ha"] == "2.6"  # kept for reference only


async def test_a_duplicate_number_in_one_organization_is_409(gis_client, leshoz, contours_layer):
    payload = {
        "layer_id": str(contours_layer.id),
        "organization_id": str(leshoz.id),
        "number": "dup-1",
        "kind": "contour",
    }
    first = await gis_client.post("/api/v1/gis/contours", json=payload)
    assert first.status_code == 201
    second = await gis_client.post("/api/v1/gis/contours", json=payload)
    assert second.status_code == 409


async def test_an_applicant_cannot_create_a_contour(applicant_client, leshoz, contours_layer):
    """Decision #14: the applicant never draws geometry."""
    resp = await applicant_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "x-1",
            "kind": "contour",
        },
    )
    assert resp.status_code == 403


async def test_a_draft_version_can_be_edited_but_a_published_one_cannot(
    gis_client, published_contour
):
    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{published_contour.contour_id}/versions/{published_contour.id}",
        json={"accuracy_m": "3.0"},
    )
    assert resp.status_code == 409


async def test_malformed_geometry_is_422_not_500(gis_client, leshoz, contours_layer):
    """Task 3 ruling: ST_GeomFromGeoJSON's XX000 on malformed input must reach the
    caller as a clean 422 (ERR-GIS-001), never as an unhandled 500 — and the
    session it poisons must not cause a second failure (audit, or otherwise) on
    the way out."""
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "bad-geom-1",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    resp = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"geom": {"not": "geojson"}, "source": "survey"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-001"
    assert body["error"]["details"]["reason"] == "unreadable_geometry"


async def test_a_parent_id_requires_kind_subcontour(gis_client, leshoz, contours_layer):
    """The `parent_needs_subcontour` CHECK (task 1's models, flagged by task 1's
    review as the one named invariant with no test): a contour with a parent must
    be declared kind='subcontour'. This is the task that introduces `parent_id`
    through the API, so the rejection is exercised end to end here."""
    parent = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "parent-1",
            "kind": "contour",
        },
    )
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    resp = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "child-1",
            "kind": "contour",
            "parent_id": parent_id,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "parent_needs_subcontour"


async def test_a_contour_can_be_archived_via_patch(gis_client, leshoz, contours_layer):
    """`PATCH /gis/contours/{id}` (design/03) — identity-level housekeeping, not
    exercised by the brief's own four tests."""
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "arch-1",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    resp = await gis_client.patch(f"/api/v1/gis/contours/{contour_id}", json={"status": "archived"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
