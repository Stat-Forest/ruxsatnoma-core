"""The four checks of tz/07, with decision #24's correction (ST_Intersects plus an
area threshold, not ST_Overlaps) and ruling 15's tolerance."""

import uuid

from app.modules.gis import checks, repo


def _result(results, name):
    return next(r for r in results if r["check"] == name)


async def test_a_clean_geometry_passes_every_check(db, draft_version):
    results = await checks.run_checks(db, version_id=draft_version.id)
    assert _result(results, "validity")["result"] == "pass"
    assert _result(results, "overlap")["result"] == "pass"
    assert checks.is_blocked(results) is False


async def test_within_fund_is_skipped_while_the_layer_is_empty(db, draft_version):
    """Ruling 9 — the fund boundary has not been delivered yet (П.7)."""
    result = _result(await checks.run_checks(db, version_id=draft_version.id), "within_fund")
    assert result["result"] == "skipped"
    assert result["details"]["reason"] == "layer_empty"


async def test_within_fund_fails_once_a_boundary_exists_and_excludes_the_plot(
    db, draft_version, published_fund_boundary_elsewhere
):
    result = _result(await checks.run_checks(db, version_id=draft_version.id), "within_fund")
    assert result["result"] == "fail"


async def test_within_fund_passes_when_a_contour_straddles_two_adjoining_fund_polygons(
    db, contours_layer, leshoz
):
    """Task-4 review, finding 1: the fund boundary arrives as many polygons
    (151 for Burchmulla alone) — a contour that legitimately sits inside the
    fund but straddles the seam between two adjoining fund polygons is inside
    neither one individually. `bool_or(ST_Within(v.geom, f.geom))` against
    each row separately would report this as `outside_forest_fund`; the fix
    unions only the fund features that intersect the contour first."""
    from tests.modules.gis.conftest import (
        box_wkt,
        make_contour,
        make_feature,
        make_version,
        random_anchor,
    )

    lon, lat = random_anchor()
    layer = await repo.layer_by_code(db, "forest_fund")
    assert layer is not None
    await make_feature(db, layer, box_wkt(lon, lat))  # fund polygon A
    await make_feature(
        db, layer, box_wkt(lon + 0.01, lat)
    )  # fund polygon B, adjoining A's east edge

    contour = await make_contour(db, contours_layer, leshoz)
    # Straddles the seam at lon + 0.01, half in A and half in B.
    version = await make_version(db, contour.id, box_wkt(lon + 0.005, lat))

    result = _result(await checks.run_checks(db, version_id=version.id), "within_fund")
    assert result["result"] == "pass"


async def test_a_shared_border_is_not_an_overlap(
    db, neighbouring_published_contour, draft_version_touching_it
):
    """Two contours sharing an edge intersect on a line of zero area — ruling 15."""
    result = _result(
        await checks.run_checks(db, version_id=draft_version_touching_it.id), "overlap"
    )
    assert result["result"] == "pass"


async def test_a_real_overlap_is_reported_with_its_area_and_blocks(
    db, neighbouring_published_contour, draft_version_overlapping_it
):
    results = await checks.run_checks(db, version_id=draft_version_overlapping_it.id)
    result = _result(results, "overlap")
    assert result["result"] == "fail"
    assert result["details"]["items"][0]["area_m2"] > 100
    assert checks.is_blocked(results) is True


async def test_a_restriction_intersection_is_a_warning_not_a_block(
    db, draft_version, published_restriction_over_it
):
    results = await checks.run_checks(db, version_id=draft_version.id)
    assert _result(results, "restrictions")["result"] == "warning"
    assert checks.is_blocked(results) is False


async def test_an_expired_fire_ban_is_ignored(db, draft_version, expired_fire_ban_over_it):
    """A fire ban is a period plus a territory; last year's ban restricts nothing."""
    result = _result(await checks.run_checks(db, version_id=draft_version.id), "restrictions")
    assert result["result"] == "pass"


async def test_a_fire_ban_in_force_today_is_reported(db, draft_version, current_fire_ban_over_it):
    result = _result(await checks.run_checks(db, version_id=draft_version.id), "restrictions")
    assert result["result"] == "warning"
    assert result["details"]["items"][0]["layer"] == "fire_bans"


# --- Beyond the brief's eight: two correctness points the controller flagged
# but no given test exercises, plus a couple of endpoint-wiring smoke tests
# for the new route this task also adds. ---------------------------------------


async def test_a_new_version_does_not_overlap_its_own_contours_published_version(
    db, contours_layer, leshoz, approval_doc
):
    """Decision 7 (task-4 controller notes): `overlap` excludes the version's
    own contour — otherwise a contour would always overlap itself the moment it
    has both a published version and a new draft on top of it. Built at a
    random, isolated location (conftest.py's `random_box_wkt`) rather than
    reusing `published_contour` (box_wkt(69.9, 41.5)): the shared test DB has
    other, unrelated published contours sitting on that exact box (see
    `draft_version`'s own note), which would make this assertion pass or fail
    for the wrong reason."""
    from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt

    wkt = random_box_wkt()
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(db, contour.id, wkt, status="published", approval_doc_id=approval_doc.id)
    version = await make_version(db, contour.id, wkt, version_no=2)
    result = _result(await checks.run_checks(db, version_id=version.id), "overlap")
    assert result["result"] == "pass"


async def test_a_draft_restriction_is_not_reported(db, draft_version):
    """The restrictions candidate query filters `f.status = 'published'` — a
    draft (unpublished) restriction feature must not produce a warning. Placed
    at `draft_version`'s own geometry (`version_wkt`), not a second literal, so
    a wiring mistake can't hide behind "they never actually overlapped"."""
    from tests.modules.gis.conftest import make_feature, version_wkt

    layer = await repo.layer_by_code(db, "restrictions")
    assert layer is not None
    await make_feature(db, layer, await version_wkt(db, draft_version), status="draft")
    result = _result(await checks.run_checks(db, version_id=draft_version.id), "restrictions")
    assert result["result"] == "pass"


async def test_the_checks_endpoint_returns_results_and_blocked(
    db, gis_client, leshoz, contours_layer
):
    from tests.modules.gis.conftest import random_box_wkt, wkt_to_geojson

    # A random, isolated box — box_wkt(69.9, 41.5) has other, unrelated
    # published contours left behind by other tests in this shared DB (see
    # conftest.py's `draft_version` note), which would make `blocked` flaky.
    geom = await wkt_to_geojson(db, random_box_wkt())

    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "checks-ep-1",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    version = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"geom": geom, "source": "survey"},
    )
    assert version.status_code == 201
    version_id = version.json()["id"]

    resp = await gis_client.post(f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/checks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is False
    assert {c["check"] for c in body["checks"]} == {
        "validity",
        "within_fund",
        "restrictions",
        "overlap",
    }


async def test_the_checks_endpoint_404s_on_a_mismatched_contour(
    gis_client, published_contour, leshoz, contours_layer
):
    other = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "checks-ep-2",
            "kind": "contour",
        },
    )
    assert other.status_code == 201
    other_contour_id = other.json()["id"]

    resp = await gis_client.post(
        f"/api/v1/gis/contours/{other_contour_id}/versions/{published_contour.id}/checks"
    )
    assert resp.status_code == 404


async def test_the_checks_endpoint_reports_a_real_overlap_with_its_area(
    db, gis_client, leshoz, contours_layer, neighbouring_published_contour
):
    """None of the other endpoint tests exercise a non-empty `details.items` —
    this confirms a `feature_id` (a `uuid.UUID` in Python) actually round-trips
    through the real HTTP response as a JSON string, not just through the
    plain-dict assertions the other checks tests make in-process."""
    from tests.modules.gis.conftest import box_wkt, wkt_to_geojson

    geom = await wkt_to_geojson(
        db, box_wkt(69.905, 41.5)
    )  # overlaps neighbouring_published_contour

    created = await gis_client.post(
        "/api/v1/gis/contours",
        json={
            "layer_id": str(contours_layer.id),
            "organization_id": str(leshoz.id),
            "number": "checks-ep-3",
            "kind": "contour",
        },
    )
    assert created.status_code == 201
    contour_id = created.json()["id"]

    version = await gis_client.post(
        f"/api/v1/gis/contours/{contour_id}/versions",
        json={"geom": geom, "source": "survey"},
    )
    assert version.status_code == 201
    version_id = version.json()["id"]

    resp = await gis_client.post(f"/api/v1/gis/contours/{contour_id}/versions/{version_id}/checks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    overlap = next(c for c in body["checks"] if c["check"] == "overlap")
    assert overlap["result"] == "fail"
    item = overlap["details"]["items"][0]
    assert item["area_m2"] > 100
    uuid.UUID(item["feature_id"])  # a valid UUID string, not a raw dict/object leaking through
