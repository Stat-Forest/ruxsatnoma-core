"""Contour identity + draft versions through the API, with the zone rule of ruling 18."""

import uuid

from app.modules.gis import repo
from tests.modules.gis.conftest import make_contour


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


async def test_an_org_scoped_specialist_cannot_create_a_contour_for_another_org(
    org_scoped_gis_client, other_leshoz, contours_layer
):
    """Final review, finding 1: migration 0010 grants gis.contours.manage to
    gis_specialist broadly, but a leshoz-level specialist's own organization_id
    zones them to their own leshoz — creating under a DIFFERENT organization
    must be refused (ERR-ACL-001), the same zone check the three sibling
    writes (update_contour, create_version, update_version) already apply to
    an existing row, applied here to the request's own organization_id."""
    resp = await org_scoped_gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(other_leshoz.id),
            "number": "cross-org-1",
            "kind": "contour",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_version_number_race_is_a_conflict_not_a_geometry_error(
    gis_client, leshoz, contours_layer, monkeypatch
):
    """Final review, finding 3: IntegrityError IS a DBAPIError subclass, and
    next_version_no is an unlocked SELECT MAX(version_no)+1 — two concurrent
    version creates on one contour can both compute the same number and
    collide on uq_contour_version_no. That must surface as a conflict, never
    as "unreadable geometry" (ERR-GIS-001, the malformed-geometry code).
    Simulated by forcing next_version_no to return an already-taken number,
    standing in for the race without needing true concurrency."""
    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "race-1",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    geom = {
        "type": "Polygon",
        "coordinates": [[[69.9, 41.5], [69.91, 41.5], [69.91, 41.51], [69.9, 41.51], [69.9, 41.5]]],
    }
    first = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"geom": geom, "source": "survey"},
    )
    assert first.status_code == 201
    assert first.json()["version_no"] == 1

    async def _stale_next_version_no(db, contour_id):
        return 1  # already taken by `first` — stands in for the race

    monkeypatch.setattr(repo, "next_version_no", _stale_next_version_no)

    second = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"geom": geom, "source": "survey"},
    )
    assert second.status_code == 409
    body = second.json()
    assert body["error"]["code"] == "ERR-GIS-005"
    assert body["error"]["details"]["reason"] == "version_conflict"


async def test_an_unknown_layer_is_404_not_a_taken_number(gis_client, leshoz):
    """`except IntegrityError` used to swallow the FK violations on
    `layer_id`/`organization_id`/`parent_id` alongside the unique violation it
    was written for, so a nonexistent layer answered
    `409 ERR-GIS-005 {"reason": "number_taken"}` — "that number is taken" for a
    layer that does not exist."""
    resp = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(uuid.uuid4()),
            "organization_id": str(leshoz.id),
            "number": f"nolayer-{uuid.uuid4().hex[:6]}",
            "kind": "contour",
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_contour_cannot_be_created_under_a_non_contour_layer(db, gis_client, leshoz):
    """Nothing checked that `layer_id` WAS the contours layer, so a contour
    could be created under `water_points` — and would still show up in
    `list_contours`, which filters by no layer at all."""
    water_points = await repo.layer_by_code(db, "water_points")
    assert water_points is not None
    resp = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(water_points.id),
            "organization_id": str(leshoz.id),
            "number": f"wrong-layer-{uuid.uuid4().hex[:6]}",
            "kind": "contour",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "not_the_contours_layer"


async def test_an_unknown_organization_is_422_not_a_taken_number(gis_client, contours_layer):
    resp = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(uuid.uuid4()),
            "number": f"noorg-{uuid.uuid4().hex[:6]}",
            "kind": "contour",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "organization_not_found"


async def test_an_imported_contour_becomes_a_subcontour(db, gis_client, leshoz, contours_layer):
    """Decision #49 ruling 11: the importer creates everything flat BECAUSE it
    refuses to guess a hierarchy from the file, and the hierarchy is set
    afterwards through `PATCH /gis/contours/{id}` with `kind` and `parent_id`.
    `ContourPatch` carried `status` alone, so that edit did not exist and an
    imported contour could never become a sub-contour — stage 7's data loading
    depends on it."""
    parent = await make_contour(db, contours_layer, leshoz)
    child = await make_contour(db, contours_layer, leshoz)

    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{child.id}",
        json={"kind": "subcontour", "parent_id": str(parent.id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "subcontour"
    assert body["parent_id"] == str(parent.id)


async def test_a_parent_without_the_subcontour_kind_is_refused(
    db, gis_client, leshoz, contours_layer
):
    """The `parent_needs_subcontour` CHECK, pre-checked against the row as it
    WILL be — otherwise the DB CHECK surfaces as an unhandled 500."""
    parent = await make_contour(db, contours_layer, leshoz)
    child = await make_contour(db, contours_layer, leshoz)

    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{child.id}", json={"parent_id": str(parent.id)}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "parent_needs_subcontour"


async def test_clearing_kind_while_a_parent_remains_is_refused(
    db, gis_client, leshoz, contours_layer
):
    """The same violation from the other direction: the pairing is checked
    against the merged row, not against the request in isolation."""
    parent = await make_contour(db, contours_layer, leshoz)
    child = await make_contour(db, contours_layer, leshoz, kind="subcontour", parent_id=parent.id)

    resp = await gis_client.patch(f"/api/v1/gis/contours/{child.id}", json={"kind": "contour"})
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "parent_needs_subcontour"


async def test_a_parent_in_another_organization_is_refused(
    db, gis_client, leshoz, other_leshoz, contours_layer
):
    """`contours.parent_id` is a plain FK with no organization condition of its
    own, so nothing below the service stops one leshoz hanging a sub-contour
    off another leshoz's row."""
    stranger = await make_contour(db, contours_layer, other_leshoz)
    child = await make_contour(db, contours_layer, leshoz)

    resp = await gis_client.patch(
        f"/api/v1/gis/contours/{child.id}",
        json={"kind": "subcontour", "parent_id": str(stranger.id)},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "parent_other_organization"


async def test_an_unknown_parent_and_a_self_parent_are_both_refused(
    db, gis_client, leshoz, contours_layer
):
    child = await make_contour(db, contours_layer, leshoz)

    unknown = await gis_client.patch(
        f"/api/v1/gis/contours/{child.id}",
        json={"kind": "subcontour", "parent_id": str(uuid.uuid4())},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["reason"] == "parent_not_found"

    itself = await gis_client.patch(
        f"/api/v1/gis/contours/{child.id}",
        json={"kind": "subcontour", "parent_id": str(child.id)},
    )
    assert itself.status_code == 422
    assert itself.json()["error"]["details"]["reason"] == "parent_is_self"


async def test_archiving_alone_never_disturbs_the_hierarchy(db, gis_client, leshoz, contours_layer):
    """`exclude_unset` keeps "not supplied" distinct from "set to null": a
    status-only PATCH must not silently detach a sub-contour from its parent."""
    parent = await make_contour(db, contours_layer, leshoz)
    child = await make_contour(db, contours_layer, leshoz, kind="subcontour", parent_id=parent.id)

    resp = await gis_client.patch(f"/api/v1/gis/contours/{child.id}", json={"status": "archived"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "archived"
    assert body["parent_id"] == str(parent.id)
    assert body["kind"] == "subcontour"
