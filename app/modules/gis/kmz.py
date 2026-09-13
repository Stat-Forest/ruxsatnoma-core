"""`GET /gis/contours/{id}/export.kmz` — one contour's published boundary as
a KMZ file (Odilxon, 2026-09-13: the application and permit cards should show
the plot on a map and hand the boundary over as KMZ, the format the Agency's
own geodata arrived in — decision #45 — and the one a forester's Google Earth
or handheld GPS opens without asking).

Written by hand rather than through GDAL's KML driver: the document is a
single `Placemark` with a `Polygon` (or a `MultiGeometry` of them), and
`pyogrio`'s write path would need the LIBKML driver for `.kmz`, which the
wheel does not promise. `xml.etree` builds the XML so a contour number like
`10517қ` or a leshoz name with `&` is escaped by the library, never by us.
`zipfile` wraps it as `doc.kml`, which is the one file name Google Earth
looks for inside a KMZ."""

import io
import zipfile
from collections.abc import Iterable
from typing import Any
from xml.etree import ElementTree as ET

KML_NS = "http://www.opengis.net/kml/2.2"
MEDIA_TYPE = "application/vnd.google-earth.kmz"

# Google Earth's own colour order is aabbggrr, not rrggbbaa: `7f00aa00` is a
# half-transparent green fill, `ff00aa00` an opaque green outline.
_STYLE_ID = "contour"
_FILL_COLOR = "7f00aa00"
_LINE_COLOR = "ff00aa00"


def _ring_coordinates(ring: Iterable[Iterable[float]]) -> str:
    """KML wants `lon,lat[,alt]` triples separated by whitespace — the same
    lon-first order GeoJSON already uses (decision #13: everything is WGS84)."""
    return " ".join(f"{float(lon)},{float(lat)},0" for lon, lat, *_ in ring)


def _polygon(parent: ET.Element, rings: list[list[list[float]]]) -> None:
    polygon = ET.SubElement(parent, "Polygon")
    for index, ring in enumerate(rings):
        boundary = ET.SubElement(polygon, "outerBoundaryIs" if index == 0 else "innerBoundaryIs")
        linear_ring = ET.SubElement(boundary, "LinearRing")
        ET.SubElement(linear_ring, "coordinates").text = _ring_coordinates(ring)


def _geometry(parent: ET.Element, geometry: dict[str, Any]) -> None:
    """A `Polygon` or a `MultiPolygon` — the two shapes `contour_versions.geom`
    can hold (`MULTIPOLYGON` column, ruling 3.6a; `ST_AsGeoJSON` answers a
    `MultiPolygon` even for one part). Anything else is a programming error
    upstream, not a client mistake, so it raises rather than answers an empty
    placemark the map would then quietly draw as nothing."""
    kind = geometry.get("type")
    coordinates: list[Any] = geometry.get("coordinates") or []
    if kind == "Polygon" and coordinates:
        _polygon(parent, coordinates)
    elif kind == "MultiPolygon" and coordinates:
        if len(coordinates) == 1:
            _polygon(parent, coordinates[0])
        else:
            multi = ET.SubElement(parent, "MultiGeometry")
            for rings in coordinates:
                _polygon(multi, rings)
    else:
        raise ValueError(f"unsupported contour geometry: {kind!r}")


def render_kml(
    *, number: str, organization_name: str, area_ha: str, geometry: dict[str, Any]
) -> bytes:
    """The KML document as bytes. `area_ha` arrives as its display string so
    this module never rounds a Decimal on its own."""
    ET.register_namespace("", KML_NS)
    kml = ET.Element(f"{{{KML_NS}}}kml")
    document = ET.SubElement(kml, "Document")
    ET.SubElement(document, "name").text = f"Kontur {number}"

    style = ET.SubElement(document, "Style", id=_STYLE_ID)
    line_style = ET.SubElement(style, "LineStyle")
    ET.SubElement(line_style, "color").text = _LINE_COLOR
    ET.SubElement(line_style, "width").text = "2"
    poly_style = ET.SubElement(style, "PolyStyle")
    ET.SubElement(poly_style, "color").text = _FILL_COLOR

    placemark = ET.SubElement(document, "Placemark")
    ET.SubElement(placemark, "name").text = f"Kontur {number}"
    ET.SubElement(placemark, "description").text = f"{organization_name} — {area_ha} ga"
    ET.SubElement(placemark, "styleUrl").text = f"#{_STYLE_ID}"
    _geometry(placemark, geometry)

    return ET.tostring(kml, encoding="utf-8", xml_declaration=True)


def render_kmz(
    *, number: str, organization_name: str, area_ha: str, geometry: dict[str, Any]
) -> bytes:
    """`render_kml` zipped as `doc.kml` — a KMZ is exactly that."""
    kml = render_kml(
        number=number, organization_name=organization_name, area_ha=area_ha, geometry=geometry
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml)
    return buffer.getvalue()
