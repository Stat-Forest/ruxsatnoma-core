"""`GET /gis/contours/{id}/export.kmz` (Odilxon, 2026-09-13): the boundary an
application or permit card hands over to Google Earth. Reads through the same
`contour_card` the map draws from, so the two rules that hide a map — no
published version, decision #178's `None` geometry — refuse the file too."""

import io
import zipfile
from xml.etree import ElementTree as ET

from sqlalchemy import func

from app.core import xlsx
from app.main import create_app
from app.modules.gis import kmz
from tests.conftest import make_client
from tests.modules.gis.conftest import box_wkt, make_contour, make_version, random_anchor

KML = "{http://www.opengis.net/kml/2.2}"


def _doc_kml(body: bytes) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.namelist() == ["doc.kml"]
        return ET.fromstring(archive.read("doc.kml"))


async def test_an_applicant_downloads_the_published_boundary_as_kmz(
    applicant_client, published_contour, leshoz
):
    """One placemark, named by the contour number, whose polygon is the very
    ring the card's own GeoJSON carries — lon,lat order, the way Google Earth
    reads it. The filename carries the number so a forester with ten files
    can tell them apart."""
    card = await applicant_client.get(f"/api/v1/gis/contours/{published_contour.contour_id}")
    assert card.status_code == 200, card.text
    number = card.json()["number"]
    ring = card.json()["geometry"]["coordinates"][0][0]

    resp = await applicant_client.get(
        f"/api/v1/gis/contours/{published_contour.contour_id}/export.kmz"
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == kmz.MEDIA_TYPE
    assert f"kontur-{number}.kmz" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"

    root = _doc_kml(resp.content)
    assert root.tag == f"{KML}kml"
    placemarks = root.findall(f".//{KML}Placemark")
    assert len(placemarks) == 1
    assert placemarks[0].findtext(f"{KML}name") == f"Kontur {number}"
    description = placemarks[0].findtext(f"{KML}description") or ""
    assert xlsx.localized(leshoz.name, "uz_latn") in description
    assert card.json()["area_ha"] in description
    coordinates = placemarks[0].findtext(f".//{KML}outerBoundaryIs//{KML}coordinates") or ""
    triples = [tuple(float(v) for v in triple.split(",")) for triple in coordinates.split()]
    assert [(lon, lat) for lon, lat, _ in triples] == [(lon, lat) for lon, lat in ring]


async def test_a_contour_without_geometry_is_refused_not_emptied(
    applicant_client, published_contour_without_geometry
):
    """Decision #178: a contour filed by requisites alone has a card and a
    price but no boundary — the button hides on that card, and a direct call
    gets a NAMED 404 rather than a KMZ that opens as nothing."""
    resp = await applicant_client.get(
        f"/api/v1/gis/contours/{published_contour_without_geometry.contour_id}/export.kmz"
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR-GIS-007"


async def test_a_leshoz_with_gis_enabled_off_hands_out_no_boundary(
    db, applicant_client, leshoz, contours_layer, approval_doc
):
    """The switch is authoritative even over a row that DOES carry real
    geometry, exactly as it is for the card (`test_no_geometry.py`): what the
    map may not show, the file may not carry."""
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

    resp = await applicant_client.get(f"/api/v1/gis/contours/{contour.id}/export.kmz")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR-GIS-007"


async def test_an_unpublished_contour_has_no_boundary_to_export(applicant_client, draft_contour):
    """Same rule as the card: `ERR-SYS-003`, the contour is not there yet for
    a reader — nothing distinguishes "no such contour" from "not published"."""
    resp = await applicant_client.get(f"/api/v1/gis/contours/{draft_contour.contour_id}/export.kmz")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "ERR-SYS-003"


async def test_the_route_needs_a_signed_in_user(published_contour):
    """Same gate as the card and the feature layer: no permission code, but a
    session — a boundary is not an open dataset."""
    async with make_client(create_app(), lifespan=True) as anonymous:
        resp = await anonymous.get(
            f"/api/v1/gis/contours/{published_contour.contour_id}/export.kmz"
        )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "ERR-AUTH-002"


def test_a_multipolygon_with_several_parts_becomes_a_multigeometry():
    """`ST_AsGeoJSON` answers `MultiPolygon` for every row; one part flattens
    to a plain `Polygon` (what Google Earth's own exports look like), more
    than one keeps every part, holes included."""
    square = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]
    hole = [[0.2, 0.2], [0.2, 0.4], [0.4, 0.4], [0.4, 0.2], [0.2, 0.2]]
    far = [[5.0, 5.0], [5.0, 6.0], [6.0, 6.0], [6.0, 5.0], [5.0, 5.0]]
    root = ET.fromstring(
        kmz.render_kml(
            number="10517қ",
            organization_name="Burchmulla & Co",
            area_ha="2.5000",
            geometry={"type": "MultiPolygon", "coordinates": [[square, hole], [far]]},
        )
    )
    polygons = root.findall(f".//{KML}MultiGeometry/{KML}Polygon")
    assert len(polygons) == 2
    assert polygons[0].find(f"{KML}innerBoundaryIs") is not None
    assert polygons[1].find(f"{KML}innerBoundaryIs") is None
    assert root.findtext(f".//{KML}Placemark/{KML}name") == "Kontur 10517қ"
    assert "Burchmulla & Co" in (root.findtext(f".//{KML}description") or "")

    single = ET.fromstring(
        kmz.render_kml(
            number="1",
            organization_name="x",
            area_ha="1",
            geometry={"type": "MultiPolygon", "coordinates": [[square]]},
        )
    )
    assert single.find(f".//{KML}MultiGeometry") is None
    assert single.find(f".//{KML}Placemark/{KML}Polygon") is not None
