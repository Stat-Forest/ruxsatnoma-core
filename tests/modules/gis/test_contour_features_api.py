"""`GET /gis/contours/features` — the published contour layer as GeoJSON, so a
map can draw every parcel before the applicant has picked one.

The sibling of `GET /gis/contours` (paged, no geometry) tested in
`test_read_api.py`. What is asserted here is the part a list cannot have:
geometry in the response, a viewport that actually filters, and the same
visibility rules the list already obeys — a map that showed one contour more
than the list would be a scoping leak with a friendlier face.
"""

import uuid

import pytest
from sqlalchemy import func

from app.main import create_app
from tests.conftest import make_client
from tests.modules.gis.conftest import box_wkt, make_contour, make_version, random_anchor


async def _publish_at(db, layer, org, approval_doc, lon: float, lat: float):
    """One published contour whose geometry sits exactly at (lon, lat), so a
    bbox assertion can be about position rather than about luck."""
    contour = await make_contour(db, layer, org)
    await make_version(
        db,
        contour.id,
        box_wkt(lon, lat),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    return contour


def _features_by_id(body: dict) -> dict[str, dict]:
    return {f["properties"]["contour_id"]: f for f in body["features"]}


async def test_the_collection_carries_the_geometry_a_map_needs(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """The whole point of the endpoint: `GET /gis/contours` answers the same
    contours with no geometry at all, so a map drawing from it would have
    nothing to draw."""
    lon, lat = random_anchor()
    contour = await _publish_at(db, contours_layer, leshoz, approval_doc, lon, lat)
    await db.commit()

    resp = await applicant_client.get(f"/api/v1/gis/contours/features?organization_id={leshoz.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["type"] == "FeatureCollection"
    feature = _features_by_id(body)[str(contour.id)]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "MultiPolygon"
    assert feature["geometry"]["coordinates"], "geometry must not be an empty shell"
    assert feature["properties"]["number"] == contour.number
    assert feature["properties"]["area_ha"] == "92.0000"


async def test_a_viewport_excludes_what_is_outside_it(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """A map asks for what its screen shows. Without this the endpoint would
    answer the whole country on every pan — the reason `bbox` exists on the
    list endpoint too."""
    lon, lat = random_anchor()
    inside = await _publish_at(db, contours_layer, leshoz, approval_doc, lon, lat)
    # Ten degrees away: far outside the box below, and outside the 0.01-degree
    # square `box_wkt` draws, without depending on how big that square is.
    outside = await _publish_at(db, contours_layer, leshoz, approval_doc, lon + 10, lat + 10)
    await db.commit()

    bbox = f"{lon - 0.05},{lat - 0.05},{lon + 0.05},{lat + 0.05}"
    resp = await applicant_client.get(f"/api/v1/gis/contours/features?bbox={bbox}")
    assert resp.status_code == 200, resp.text
    ids = set(_features_by_id(resp.json()))

    assert str(inside.id) in ids
    assert str(outside.id) not in ids


async def test_a_draft_contour_is_absent_from_the_map(
    applicant_client, leshoz, published_contour, draft_contour
):
    """Published versions only, the same join `list_contours` makes (decision
    6): a draft has no geometry of record yet, so drawing it would put a line
    on a citizen's map around a parcel nobody has approved."""
    resp = await applicant_client.get(f"/api/v1/gis/contours/features?organization_id={leshoz.id}")
    ids = set(_features_by_id(resp.json()))

    assert str(published_contour.contour_id) in ids
    assert str(draft_contour.contour_id) not in ids


async def test_the_map_and_the_list_agree_on_what_is_visible(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """The two endpoints are built from the same predicates on purpose. If they
    ever disagree, one of them is either hiding a parcel the caller may see or
    showing one they may not — and this is the assertion that fails first."""
    lon, lat = random_anchor()
    for offset in range(3):
        await _publish_at(db, contours_layer, leshoz, approval_doc, lon + offset * 0.5, lat)
    await db.commit()

    listed = await applicant_client.get(
        f"/api/v1/gis/contours?organization_id={leshoz.id}&page_size=100"
    )
    drawn = await applicant_client.get(f"/api/v1/gis/contours/features?organization_id={leshoz.id}")

    assert {item["id"] for item in listed.json()["items"]} == set(_features_by_id(drawn.json()))


async def test_the_literal_features_is_not_read_as_a_contour_id(applicant_client):
    """FastAPI matches routes in declaration order, and `/contours/{contour_id}`
    is declared right below this endpoint. Move it above and every call here
    becomes a 422 complaining about a uuid nobody sent — a failure mode that
    looks like a client bug and is not one. This test is the tripwire."""
    resp = await applicant_client.get("/api/v1/gis/contours/features")

    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "FeatureCollection"


async def test_an_anonymous_caller_gets_nothing():
    """Same gate as the list and the card: no permission code, but a session IS
    required. The parcels of the forest fund are not an open dataset — and this
    endpoint hands out geometry in bulk, which is exactly the shape somebody
    would be tempted to leave open for "just the map"."""
    async with make_client(create_app(), lifespan=True) as anonymous:
        resp = await anonymous.get("/api/v1/gis/contours/features")

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "ERR-AUTH-002"


@pytest.mark.parametrize(
    "bbox",
    ["1,2,3", "a,b,c,d", "nan,nan,nan,nan", "10,10,5,5", "-200,0,200,0"],
    ids=["too-few", "not-numeric", "not-finite", "min-past-max", "out-of-range"],
)
async def test_a_malformed_viewport_is_refused_not_crashed(applicant_client, bbox):
    """`_parse_bbox`'s guarantee, reasserted on this route: bad input is a 422,
    never a 500. `nan` is the one that matters — every comparison against it is
    False, so it passes a naive range check and reaches PostGIS, which raises,
    and `app/main.py` has no `DBAPIError` handler."""
    resp = await applicant_client.get(f"/api/v1/gis/contours/features?bbox={bbox}")

    assert resp.status_code == 422, resp.text


async def test_a_leshoz_scoped_actor_sees_only_their_own_parcels(
    db, org_scoped_gis_client, leshoz, other_leshoz, contours_layer, approval_doc
):
    """Zone scoping, built by the same `zone_filter` call the list uses. A read
    path needs the zone gate as much as a write one does, and `gis_client` is
    republic-wide on purpose — `org_scoped_gis_client` is the shape that can
    actually be under-served, zoned to `leshoz` and nothing else."""
    lon, lat = random_anchor()
    mine = await _publish_at(db, contours_layer, leshoz, approval_doc, lon, lat)
    theirs = await _publish_at(db, contours_layer, other_leshoz, approval_doc, lon + 0.5, lat)
    await db.commit()

    resp = await org_scoped_gis_client.get("/api/v1/gis/contours/features")
    assert resp.status_code == 200, resp.text
    ids = set(_features_by_id(resp.json()))

    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


async def test_an_unknown_organization_filter_answers_an_empty_collection(applicant_client):
    """Not a 404: the filter is a viewport-shaped narrowing, and a map asking
    about an area with nothing in it wants an empty collection, not an error
    to special-case."""
    resp = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={uuid.uuid4()}"
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["features"] == []
    assert resp.json()["truncated"] is False


async def test_tolerance_answers_a_simplified_overview_of_the_same_contours(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """`?tolerance=` is for a map zoomed out to a region: the same contours,
    each geometry simplified to the tolerance and written with five decimals,
    so thousands of parcels weigh what hundreds of detailed ones do. A
    polygon with a jagged edge loses the jag; a tolerance out of range is a
    422, not a silently detailed answer."""
    lon, lat = random_anchor()
    # A rectangle with sixteen tiny notches along its northern edge — every
    # one of them is under a metre and vanishes at a tolerance of 0.0005°.
    step = 0.01 / 16
    notched = [(lon + i * step, lat + 0.01 + (0.000004 if i % 2 else 0)) for i in range(17)]
    ring = [(lon, lat), (lon + 0.01, lat)] + [(x, y) for x, y in reversed(notched)] + [(lon, lat)]
    wkt = "MULTIPOLYGON(((" + ",".join(f"{x} {y}" for x, y in ring) + ")))"
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db,
        contour.id,
        wkt,
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    await db.commit()

    detailed = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}"
    )
    overview = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}&tolerance=0.0005"
    )
    assert overview.status_code == 200, overview.text
    detailed_geom = _features_by_id(detailed.json())[str(contour.id)]["geometry"]
    overview_geom = _features_by_id(overview.json())[str(contour.id)]["geometry"]
    assert overview_geom["type"] == detailed_geom["type"] == "MultiPolygon"
    detailed_ring = detailed_geom["coordinates"][0][0]
    overview_ring = overview_geom["coordinates"][0][0]
    assert len(detailed_ring) == len(ring)
    assert len(overview_ring) == 5, overview_ring
    assert all(len(str(v).split(".")[-1]) <= 5 for point in overview_ring for v in point)

    too_coarse = await applicant_client.get(
        f"/api/v1/gis/contours/features?organization_id={leshoz.id}&tolerance=1"
    )
    assert too_coarse.status_code == 422
