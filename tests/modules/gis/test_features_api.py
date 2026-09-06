"""The layers a contour is checked against (tz/07 items 8, 9, 14). A fire ban is a
period plus a territory, so validity dates are part of the object, not metadata."""

import uuid
from datetime import date, timedelta


async def test_a_fire_ban_is_created_with_its_period_and_published(
    gis_client, restrictions_polygon
):
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={
            "geom": restrictions_polygon,
            "name": {
                "uz_cyrl": "Ёнғин тақиқи",
                "uz_latn": "Yongʻin taqiqi",
                "ru": "Пожарный запрет",
            },
            "valid_from": str(date.today()),
            "valid_to": str(date.today() + timedelta(days=30)),
            "props": {"order_no": "12-ф"},
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    feature_id = created.json()["id"]
    published = await gis_client.post(
        f"/api/v1/gis/layers/fire_bans/features/{feature_id}/publish",
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"


async def test_a_point_is_refused_by_a_polygon_layer(gis_client):
    resp = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": {"type": "Point", "coordinates": [69.9, 41.5]}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "geometry_type_mismatch"


async def test_a_point_is_accepted_by_the_water_points_layer(gis_client):
    resp = await gis_client.post(
        "/api/v1/gis/layers/water_points/features",
        json={"geom": {"type": "Point", "coordinates": [69.9, 41.5]}},
    )
    assert resp.status_code == 201


async def test_an_end_date_before_the_start_is_refused(gis_client, restrictions_polygon):
    resp = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon, "valid_from": "2026-09-01", "valid_to": "2026-08-01"},
    )
    assert resp.status_code == 422


async def test_features_require_the_layers_permission(applicant_client, restrictions_polygon):
    resp = await applicant_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon},
    )
    assert resp.status_code == 403


# --- Task-6 controller decisions: zone scoping for a nullable organization_id
# (decision 3), the draft -> published -> archived lifecycle with no review
# step (decision 5), and the DB CHECK backstop a partial PATCH can still hit
# even though FeatureIn/FeaturePatch already validate the same-request case
# (decision 4) -----------------------------------------------------------


async def test_a_zone_scoped_actor_cannot_create_a_republic_wide_feature(
    org_scoped_layers_client, restrictions_polygon
):
    """decision 3: organization_id=None is a republic-wide object — only an
    actor with no organization of their own may create one, or a leshoz
    specialist could publish a nationwide fire ban through their own zone."""
    resp = await org_scoped_layers_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_zone_scoped_actor_creates_a_feature_for_their_own_organization(
    org_scoped_layers_client, restrictions_polygon, leshoz
):
    resp = await org_scoped_layers_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon, "organization_id": str(leshoz.id)},
    )
    assert resp.status_code == 201
    assert resp.json()["organization_id"] == str(leshoz.id)


async def test_a_region_scoped_actor_cannot_create_a_republic_wide_feature(
    region_scoped_layers_client, restrictions_polygon
):
    """Final review on task 6: `_assert_feature_zone` originally checked
    `organization_id` alone. `Zone` has three independent axes
    (`app/core/abac.py`) — an actor scoped to a REGION but no organization
    passed the old check as "republic-level" and could reach every leshoz in
    their region (or the whole country) through a nominally republic-wide
    fire ban. The gate must require the whole zone empty, not just one axis
    of it."""
    resp = await region_scoped_layers_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_genuinely_republic_level_actor_creates_a_republic_wide_feature(
    gis_client, restrictions_polygon
):
    """The positive side of the same boundary: an actor whose zone is empty on
    EVERY axis — region, district AND organization; `gis_client` is exactly
    this — may still create a republic-wide feature."""
    resp = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon},
    )
    assert resp.status_code == 201
    assert resp.json()["organization_id"] is None


async def test_an_unknown_layer_code_is_404(gis_client, restrictions_polygon):
    resp = await gis_client.post(
        "/api/v1/gis/layers/nope/features", json={"geom": restrictions_polygon}
    )
    assert resp.status_code == 404


async def test_an_unknown_feature_is_404(gis_client):
    resp = await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{uuid.uuid4()}/publish")
    assert resp.status_code == 404


async def test_a_draft_feature_is_patched(gis_client, restrictions_polygon):
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features", json={"geom": restrictions_polygon}
    )
    feature_id = created.json()["id"]
    patched = await gis_client.patch(
        f"/api/v1/gis/layers/fire_bans/features/{feature_id}",
        json={"props": {"order_no": "7-ф"}},
    )
    assert patched.status_code == 200
    assert patched.json()["props"] == {"order_no": "7-ф"}


async def test_a_published_feature_cannot_be_patched(gis_client, restrictions_polygon):
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features", json={"geom": restrictions_polygon}
    )
    feature_id = created.json()["id"]
    await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{feature_id}/publish")
    patched = await gis_client.patch(
        f"/api/v1/gis/layers/fire_bans/features/{feature_id}",
        json={"props": {"order_no": "7-ф"}},
    )
    assert patched.status_code == 409
    assert patched.json()["error"]["details"]["reason"] == "not_draft"


async def test_patching_only_one_side_of_the_validity_period_hits_the_db_check(
    gis_client, restrictions_polygon
):
    """decision 4: FeatureIn/FeaturePatch's own model_validator only ever sees
    ONE request at a time — a PATCH that moves valid_to before an EXISTING
    valid_from it never touches can only be caught by the
    validity_period_valid DB CHECK, converted here to the same clean 422
    instead of a raw 500."""
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": restrictions_polygon, "valid_from": str(date.today())},
    )
    feature_id = created.json()["id"]
    patched = await gis_client.patch(
        f"/api/v1/gis/layers/fire_bans/features/{feature_id}",
        json={"valid_to": str(date.today() - timedelta(days=1))},
    )
    assert patched.status_code == 422
    assert patched.json()["error"]["details"]["reason"] == "validity_period_invalid"


async def test_malformed_geometry_is_refused_cleanly(gis_client):
    """`ST_GeomFromGeoJSON`'s own parse failure (not just a type mismatch) must
    reach the caller as a clean 422, never the raw 500 an uncaught DBAPIError
    would otherwise produce — same reasoning as `create_version`'s own
    unreadable_geometry branch."""
    resp = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": {"type": "Polygon", "coordinates": "not-a-list"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "unreadable_geometry"


async def test_an_empty_geometry_is_refused_cleanly(gis_client):
    """A syntactically valid but EMPTY GeoJSON (parses fine, carries nothing)
    is a different failure from a type mismatch — `feature_geometry_type`
    reports it as `None`, never as some arbitrary geometry type."""
    resp = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features",
        json={"geom": {"type": "GeometryCollection", "geometries": []}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "empty_geometry"


async def test_a_draft_feature_cannot_be_archived_directly(gis_client, restrictions_polygon):
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features", json={"geom": restrictions_polygon}
    )
    feature_id = created.json()["id"]
    resp = await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{feature_id}/archive")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["reason"] == "bad_transition"


async def test_a_published_feature_is_archived_and_stays_archived(gis_client, restrictions_polygon):
    created = await gis_client.post(
        "/api/v1/gis/layers/fire_bans/features", json={"geom": restrictions_polygon}
    )
    feature_id = created.json()["id"]
    await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{feature_id}/publish")
    archived = await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{feature_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    again = await gis_client.post(f"/api/v1/gis/layers/fire_bans/features/{feature_id}/archive")
    assert again.status_code == 409
