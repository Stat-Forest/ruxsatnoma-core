"""What 3.7 (norms) and 3.9 (applications) — and the applicant picking a plot —
actually read. S_available is a declared placeholder until permits land (ruling 14)."""

import pytest
from sqlalchemy import func

from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt


async def test_an_applicant_sees_published_contours_only(
    applicant_client, leshoz, published_contour, draft_contour
):
    """Filtered to the fixture's own (fresh) leshoz: the list is paged now, and
    the shared, persistent test DB carries every published contour every past
    run of this suite left behind — page 1 of 20 says nothing about this
    fixture without the filter."""
    resp = await applicant_client.get(f"/api/v1/gis/contours?organization_id={leshoz.id}")
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(published_contour.contour_id) in ids
    assert str(draft_contour.contour_id) not in ids


async def test_the_contour_list_is_paged(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """design/03's own convention (`?page=1&page_size=20`, max 100), the same
    `Page[T]` envelope `/admin/users` uses. Unbounded, this answered every
    published contour in the country to any authenticated caller — ~13,500 rows
    once the leshozes land, and an applicant picking a plot is exactly who
    reaches it."""
    for _ in range(3):
        contour = await make_contour(db, contours_layer, leshoz)
        await make_version(
            db,
            contour.id,
            random_box_wkt(),
            status="published",
            approval_doc_id=approval_doc.id,
            published_at=func.now(),
        )

    first = await applicant_client.get(
        f"/api/v1/gis/contours?organization_id={leshoz.id}&page=1&page_size=2"
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] == 3
    assert (body["page"], body["page_size"]) == (1, 2)
    assert len(body["items"]) == 2

    second = await applicant_client.get(
        f"/api/v1/gis/contours?organization_id={leshoz.id}&page=2&page_size=2"
    )
    assert len(second.json()["items"]) == 1
    assert not {i["id"] for i in body["items"]} & {i["id"] for i in second.json()["items"]}

    over_cap = await applicant_client.get("/api/v1/gis/contours?page_size=101")
    assert over_cap.status_code == 422


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


async def test_the_contour_card_reports_a_measured_zero_on_an_untaken_contour(
    applicant_client, published_contour
):
    """Ruling 14, second half. Until 3.11a this asserted `occupancy_source ==
    "none"` — the placeholder that said "no permits module yet, do not read this
    zero as a measurement". `permits.service.occupancy_provider` is now
    registered (`app/event_subscriptions.py`), so the same three figures mean
    something different and the test says so: the source is `"permits"`, and the
    zero is a real answer about a contour nobody holds a permit on rather than an
    admission that nobody looked. `s_available_ha` still equals `area_ha`, for the
    opposite reason than before.

    Kept rather than deleted: what a card shows for an UNTAKEN contour is exactly
    where an off-by-one in the provider's predicate would surface first."""
    resp = await applicant_client.get(f"/api/v1/gis/contours/{published_contour.contour_id}")
    body = resp.json()
    assert body["occupancy_source"] == "permits"
    assert body["occupied_ha"] == "0.0000"
    assert body["s_available_ha"] == body["area_ha"]


async def test_two_registered_occupancy_providers_are_summed_over_a_whole_page(
    db, published_contour, draft_contour
):
    """The seam 3.11 will fill. Two providers, so the summation path is really
    exercised, and a two-contour page, so the BATCH shape is: the provider is
    asked once for every id on the page and answers with a mapping. The
    per-contour signature it replaced made `list_contours` one query per row —
    3.11's first registration would have turned a page of 20 into 20
    round-trips. A provider may answer about fewer contours than it was asked
    about (`second` below knows nothing about the draft one); a missing key
    counts as zero."""
    from decimal import Decimal

    from app.modules.gis import service

    asked: list[list] = []

    async def first(db_, contour_ids):
        asked.append(list(contour_ids))
        return {contour_id: Decimal("10.0") for contour_id in contour_ids}

    async def second(db_, contour_ids):
        return {published_contour.contour_id: Decimal("2.5")}

    service.OCCUPANCY_PROVIDERS.extend([first, second])
    try:
        ids = [published_contour.contour_id, draft_contour.contour_id]
        totals, source = await service.occupancy_map(db, ids)
        assert source == "permits"
        assert totals[published_contour.contour_id] == Decimal("12.5000")
        assert totals[draft_contour.contour_id] == Decimal("10.0000")
        assert asked == [ids]  # one call for the whole page, not one per row

        occupied, source = await service.occupancy_ha(db, published_contour.contour_id)
        assert (occupied, source) == (Decimal("12.5000"), "permits")
    finally:
        service.OCCUPANCY_PROVIDERS.remove(first)
        service.OCCUPANCY_PROVIDERS.remove(second)


async def test_the_public_surface_for_norms_and_applications_exists(db, published_contour):
    """The module docstring promises `published_version()`, `list_contours()`
    and `run_checks()` as gis's surface for levels 3+; only `list_contours`
    was actually there. Without these two, 3.7 would have to import
    `gis.repo`/`gis.checks` and break the module-boundary rule."""
    from app.modules.gis import service

    version = await service.published_version(db, published_contour.contour_id)
    assert version is not None and version.id == published_contour.id

    results = await service.run_checks(db, published_contour.id)
    assert {r["check"] for r in results} == {"validity", "within_fund", "overlap", "restrictions"}


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


async def test_an_operator_lists_an_imports_draft_features_and_publishes_one(
    gis_client, processed_restrictions_import
):
    """The one gap that made a `forest_fund` delivery unpublishable: an import
    writes non-contour features at `status='draft'`, the batch endpoints refuse
    a non-contour batch, the per-feature publish route needs an id — and no
    endpoint returned those ids, because `features_geojson` hard-coded
    `status == 'published'` and `GET /gis/imports/{id}` returns only counters.
    `checks._within_fund` stays `skipped` until that layer has published
    features, so the stage's own gating check could not be switched on through
    its own API."""
    drafts = await gis_client.get(
        "/api/v1/gis/layers/restrictions/features"
        f"?status=draft&import_id={processed_restrictions_import.id}"
    )
    assert drafts.status_code == 200
    features = drafts.json()["features"]
    assert features, "the import's own draft features must be reachable by id"

    feature_id = features[0]["id"]
    published = await gis_client.post(
        f"/api/v1/gis/layers/restrictions/features/{feature_id}/publish"
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    still_draft = await gis_client.get(
        "/api/v1/gis/layers/restrictions/features"
        f"?status=draft&import_id={processed_restrictions_import.id}"
    )
    assert feature_id not in {f["id"] for f in still_draft.json()["features"]}


async def test_an_applicant_may_not_ask_for_drafts(applicant_client, published_fire_ban):
    """`fire_bans` is a PUBLIC layer, so the applicant passes the layer gate —
    the status gate is what has to refuse them, not the layer's own visibility."""
    resp = await applicant_client.get("/api/v1/gis/layers/fire_bans/features?status=draft")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_an_unknown_status_is_422(gis_client):
    resp = await gis_client.get("/api/v1/gis/layers/fire_bans/features?status=nonsense")
    assert resp.status_code == 422
