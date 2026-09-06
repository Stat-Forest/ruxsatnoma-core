"""`POST /gis/contours/{parent_id}/split` (decision #91): one parent contour
into two subcontours, atomically. The parent's own row and its published
version are read but never written by this endpoint — every test that checks
"the parent is unaffected" re-reads it through the ordinary card route rather
than trusting that nothing happened."""

from decimal import Decimal

from sqlalchemy import func, select

from app.modules.audit.models import AuditLog
from app.modules.gis import service
from app.modules.gis.models import Contour
from tests.modules.gis.conftest import box_wkt, make_contour, make_version, wkt_to_geojson

# `published_contour` (conftest.py) sits at box_wkt(69.9, 41.5): lon [69.9, 69.91],
# lat [41.5, 41.51], ~92 ha. Every geometry below is built against that exact
# box so a test can assert on it without recomputing the anchor each time.
_MIN_LON, _MIN_LAT, _SIZE = 69.9, 41.5, 0.01
_MID_LON = _MIN_LON + _SIZE / 2


def _band_wkt(lon0: float, lon1: float) -> str:
    """A vertical band of the box, from `lon0` to `lon1`, full latitude span."""
    y0, y1 = _MIN_LAT, _MIN_LAT + _SIZE
    return f"POLYGON(({lon0} {y0}, {lon1} {y0}, {lon1} {y1}, {lon0} {y1}, {lon0} {y0}))"


def _west_half_wkt() -> str:
    return _band_wkt(_MIN_LON, _MID_LON)


def _east_half_wkt() -> str:
    return _band_wkt(_MID_LON, _MIN_LON + _SIZE)


async def _split_payload(db, piece_a_wkt: str, piece_b_wkt: str) -> dict:
    return {
        "piece_a": {"number": "split-a", "geom": await wkt_to_geojson(db, piece_a_wkt)},
        "piece_b": {"number": "split-b", "geom": await wkt_to_geojson(db, piece_b_wkt)},
        "source": "survey",
    }


async def test_a_specialist_splits_a_published_contour_into_two_draft_subcontours(
    db, gis_client, published_contour
):
    """The happy path: two subcontours, two draft versions, one atomic call —
    and the parent's own row and published version are untouched (decision
    #91)."""
    parent_id = published_contour.contour_id
    payload = await _split_payload(db, _west_half_wkt(), _east_half_wkt())
    resp = await gis_client.post(f"/api/v1/gis/contours/{parent_id}/split", json=payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["parent_id"] == str(parent_id)

    for piece_key, number in (("piece_a", "split-a"), ("piece_b", "split-b")):
        piece = body[piece_key]
        assert piece["contour"]["kind"] == "subcontour"
        assert piece["contour"]["parent_id"] == str(parent_id)
        assert piece["contour"]["number"] == number
        assert piece["version"]["status"] == "draft"
        assert piece["version"]["version_no"] == 1
        # Each half is ~46 ha; the blade-free cut here should land close to it.
        assert 40 < float(piece["version"]["area_ha"]) < 52

    # The parent's own card is unaffected: same published version, same area.
    card = await gis_client.get(f"/api/v1/gis/contours/{parent_id}")
    assert card.status_code == 200
    card_body = card.json()
    assert card_body["version_id"] == str(published_contour.id)
    assert card_body["area_ha"] == "92"

    # One extra audit row ties both pairs together under the parent's id.
    audit_row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "contour.split", AuditLog.object_id == parent_id
            )
        )
    ).scalar_one()
    assert audit_row.new_value is not None
    assert audit_row.new_value["piece_a_contour_id"] == body["piece_a"]["contour"]["id"]
    assert audit_row.new_value["piece_b_contour_id"] == body["piece_b"]["contour"]["id"]


async def test_an_applicant_cannot_split_a_contour(applicant_client, db, published_contour):
    """Decision #14 territory: an applicant never draws or edits geometry,
    splitting included."""
    payload = await _split_payload(db, _west_half_wkt(), _east_half_wkt())
    resp = await applicant_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 403


async def test_splitting_a_contour_outside_the_actors_zone_is_refused(
    db, org_scoped_gis_client, other_leshoz, contours_layer, approval_doc
):
    """`org_scoped_gis_client` is zoned to `leshoz` (fixture docstring); a
    published contour that belongs to a DIFFERENT organization must be
    refused before anything about its geometry is even read — the same zone
    check `create_contour`/`create_version` already apply, applied here to
    the split's own parent lookup."""
    contour = await make_contour(db, contours_layer, other_leshoz)
    await make_version(
        db,
        contour.id,
        box_wkt(10.0, 10.0),
        status="published",
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    payload = await _split_payload(db, _band_wkt(10.0, 10.005), _band_wkt(10.005, 10.01))
    resp = await org_scoped_gis_client.post(
        f"/api/v1/gis/contours/{contour.id}/split", json=payload
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_contour_that_already_has_a_subcontour_cannot_be_split_again(
    db, gis_client, published_contour, contours_layer, leshoz
):
    """A second split would leave the hierarchy ambiguous about which pair of
    subcontours is the authoritative one — refused outright, before any
    geometry is even looked at."""
    await make_contour(
        db, contours_layer, leshoz, parent_id=published_contour.contour_id, kind="subcontour"
    )
    payload = await _split_payload(db, _west_half_wkt(), _east_half_wkt())
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-005"
    assert body["error"]["details"]["reason"] == "already_split"


async def test_a_draft_contour_has_nothing_to_split(db, gis_client, contours_layer, leshoz):
    """No published version yet — nothing for two submitted pieces to
    reconstruct."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(db, contour.id, box_wkt(20.0, 20.0), status="draft")
    payload = await _split_payload(db, _band_wkt(20.0, 20.005), _band_wkt(20.005, 20.01))
    resp = await gis_client.post(f"/api/v1/gis/contours/{contour.id}/split", json=payload)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-005"
    assert body["error"]["details"]["reason"] == "parent_not_published"


async def test_a_contour_with_an_active_permit_cannot_be_split(db, gis_client, published_contour):
    """The occupancy seam (`permits.service.occupancy_provider` in a real
    deployment; a bare fake here, matching `test_read_api.py`'s own idiom for
    exercising this seam without a full permits fixture) reports non-zero
    occupancy: something live still depends on the parent meaning the whole
    area, so the split is refused."""

    async def occupied(db_, contour_ids):
        return {published_contour.contour_id: Decimal("12.5")}

    service.OCCUPANCY_PROVIDERS.append(occupied)
    try:
        payload = await _split_payload(db, _west_half_wkt(), _east_half_wkt())
        resp = await gis_client.post(
            f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "ERR-GIS-005"
        assert body["error"]["details"]["reason"] == "active_permit_on_parent"
    finally:
        service.OCCUPANCY_PROVIDERS.remove(occupied)


async def test_a_zero_area_piece_is_refused(db, gis_client, published_contour):
    """A line has no polygonal parts at all — `ST_CollectionExtract(..., 3)`
    (the same normalisation `insert_version` stores through) leaves nothing
    behind, the same defect `test_geometry.py::
    test_a_line_is_rejected_for_the_contour_layer` exercises for a plain
    `POST .../versions`, here for one half of a split."""
    line = {
        "type": "LineString",
        "coordinates": [[_MIN_LON, _MIN_LAT], [_MID_LON, _MIN_LAT]],
    }
    payload = {
        "piece_a": {"number": "split-a", "geom": line},
        "piece_b": {"number": "split-b", "geom": await wkt_to_geojson(db, _east_half_wkt())},
        "source": "survey",
    }
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-006"
    assert body["error"]["details"]["reason"] == "piece_zero_area"
    assert body["error"]["details"]["piece"] == "a"


async def test_two_identical_pieces_are_refused_as_an_overlap(db, gis_client, published_contour):
    """Both pieces claiming the SAME ground (a client bug, or a resubmit with
    a stale drawing) is a real overlap, not a shared border — refused before
    the coverage check even runs."""
    whole = await wkt_to_geojson(db, box_wkt(_MIN_LON, _MIN_LAT))
    payload = {
        "piece_a": {"number": "split-a", "geom": whole},
        "piece_b": {"number": "split-b", "geom": whole},
        "source": "survey",
    }
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-006"
    assert body["error"]["details"]["reason"] == "pieces_overlap"
    assert body["error"]["details"]["area_m2"] > 0


async def test_two_pieces_leaving_a_gap_are_refused_as_not_covering_the_parent(
    db, gis_client, published_contour
):
    """Two adjoining bands that only cover two of the box's three vertical
    thirds — a real gap over the eastern third, not a rounding artefact: the
    two pieces barely touch each other (near-zero mutual intersection, so
    `pieces_overlap` does not fire first) but their union misses part of the
    parent."""
    third = _SIZE / 3
    west_third_wkt = _band_wkt(_MIN_LON, _MIN_LON + third)
    middle_third_wkt = _band_wkt(_MIN_LON + third, _MIN_LON + 2 * third)
    payload = await _split_payload(db, west_third_wkt, middle_third_wkt)
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "ERR-GIS-006"
    assert body["error"]["details"]["reason"] == "pieces_do_not_cover_parent"
    assert body["error"]["details"]["area_m2"] > 0


async def test_a_duplicate_piece_number_rolls_back_the_whole_split(
    db, gis_client, published_contour, contours_layer, leshoz
):
    """`create_contour`'s own `number_taken` handling (`ERR-GIS-005`, reused
    as-is by `split_contour` rather than re-implemented) fires on the SECOND
    piece here — proving the first piece's already-flushed insert is rolled
    back with it, not left as an orphaned subcontour with no sibling."""
    await make_contour(db, contours_layer, leshoz, number="split-b")
    payload = await _split_payload(db, _west_half_wkt(), _east_half_wkt())
    resp = await gis_client.post(
        f"/api/v1/gis/contours/{published_contour.contour_id}/split", json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ERR-GIS-005"
    assert resp.json()["error"]["details"]["reason"] == "number_taken"

    # Scoped to THIS test's own `leshoz`: contour numbers are unique per
    # organization (`uq_contour_number`), not globally — the shared, persistent
    # test DB may already hold an unrelated "split-a" from another test's own
    # organization (lesson: "the test database is shared, persistent, and
    # never empty").
    remaining = (
        await db.execute(
            select(func.count(Contour.id)).where(
                Contour.number == "split-a", Contour.organization_id == leshoz.id
            )
        )
    ).scalar_one()
    assert remaining == 0
