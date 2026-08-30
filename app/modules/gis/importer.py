"""Geodata file parsing (ruling 4). One library — pyogrio, i.e. GDAL — reads every
format tz/07 asks for. The bytes are written to a temporary file because GDAL
drivers open paths, not buffers; a zipped shapefile is read through GDAL's
/vsizip/ virtual filesystem (ruling 5).

This module never touches the database or the event loop: it takes bytes and
returns features plus a per-row error list. Everything geometric beyond
"reproject to 4326" is PostGIS's job — the parser reports the source SRID and
hands over raw WKB, so there is exactly ONE reprojection engine in the system
(`repo.insert_version`'s `ST_Transform(ST_SetSRID(ST_GeomFromWKB(...), :srid),
4326)`), not GDAL's and PostGIS's answers side by side.

GDAL is blocking C code: every caller reaches `parse` through
`asyncio.to_thread`, never on the event loop.
"""

import datetime as dt
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
import pyogrio.raw

# Extensions GDAL needs on disk to pick the right driver. A format absent from
# this table is refused before GDAL ever sees the bytes — notably RAR, which the
# Agency's first delivery used and GDAL has no /vsirar for (ruling 5).
_SUFFIX: dict[str, str] = {
    "shp": ".zip",
    "zip": ".zip",
    "geojson": ".geojson",
    "kml": ".kml",
    "kmz": ".kmz",
    "gpkg": ".gpkg",
    "csv": ".csv",
}

# content_type -> accepted magic prefixes, for the upload endpoint (ruling 8).
GIS_UPLOAD_TYPES: dict[str, tuple[bytes, ...]] = {
    "application/zip": (b"PK\x03\x04",),
    "application/geo+json": (b"{",),
    "application/json": (b"{",),
    "application/vnd.google-earth.kml+xml": (b"<", b"\xef\xbb\xbf<"),
    "application/vnd.google-earth.kmz": (b"PK\x03\x04",),
    "application/geopackage+sqlite3": (b"SQLite format 3\x00",),
    "text/csv": (),  # no reliable magic; the parser is the real gate
}

# pyogrio reports a CRS as an authority string ("EPSG:32642"). Anything else is
# refused rather than guessed: ruling 10 says a source without a usable CRS is an
# error, never an assumption, and a wrong guess puts a leshoz in the ocean.
_EPSG = re.compile(r"^EPSG:([0-9]+)$", re.IGNORECASE)
# GDAL's own name for lon/lat WGS84; the same datum as EPSG:4326, which is what
# PostGIS would be told anyway.
_CRS84 = ("OGC:CRS84", "urn:ogc:def:crs:OGC:1.3:CRS84")

# Returned as the SRID when parsing failed before one could be determined. The
# caller only ever reads the SRID of a parse that produced no errors.
_NO_SRID = 0

# How many bad rows a report ever carries. The list is accumulated here, stored
# whole in one `gis_imports.error_report` JSONB value and returned whole by
# `GET /gis/imports/{id}` — so an unbounded list is unbounded memory in the
# worker, an unbounded column, and a response that cannot be serialized. Under
# the 100 MB upload cap a mis-exported GeoJSON of null-geometry features is on
# the order of a million rows, which is an ACCIDENT away, not an attack.
#
# 200: an operator fixing a delivery works from the first screenful of distinct
# problems, not from row 40 000 — the Burchmulla file is 151 features, so a real
# delivery never truncates, and a file that does has one systematic defect
# repeated, whose first 200 examples say everything the 200 001st would. The
# shape (cap plus an explicit marker) is `integrations.service`'s own
# DEAD_LETTER_PAYLOAD_MAX_BYTES.
MAX_REPORT_ROWS = 200


@dataclass(slots=True)
class ParsedFeature:
    row: int
    wkb: bytes  # reprojected by PostGIS, not by us
    attributes: dict[str, Any]


@dataclass(slots=True)
class RowError:
    row: int
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        """The shape `gis_imports.error_report` stores (tz/07: "row number plus
        error type"). Built here rather than in the service so the JSONB payload
        has one definition, not one per writer."""
        return {"row": self.row, "code": self.code, "message": self.message}


def truncation_marker(omitted: int) -> RowError:
    """The final entry of a capped report (`MAX_REPORT_ROWS`). A truncated report
    must never read as a complete one — without this an operator would fix the
    200 rows they were shown and be surprised by the 201st. `row=-1` is not a
    row number: it marks an entry that describes the REPORT, not the file."""
    return RowError(
        row=-1,
        code="report_truncated",
        message=f"{omitted} further bad row(s) omitted from this report",
    )


def _scalar(value: Any) -> Any:
    """Coerce one attribute value to a JSON primitive.

    `pyogrio` returns numpy columns, and every attribute ends up in a JSONB bind
    (`layer_features.props`, `gis_imports.stats`) served by the stock
    `json.dumps` with no encoder configured — the same class of failure the
    `Decimal`/`date` lesson describes. A NULL numeric arrives as NaN, which
    `json.dumps` renders as the bare literal `NaN`: valid Python, invalid JSON,
    and rejected outright by a `jsonb` column — so non-finite floats become
    None.
    """
    if isinstance(value, np.datetime64):
        # .item() would give a datetime/int depending on the unit; the ISO text
        # is what a JSONB column can actually hold.
        return None if np.isnat(value) else str(value)
    if isinstance(value, np.generic):
        value = value.item()  # np.str_ -> str, np.int32 -> int, np.bool -> bool
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if value is None or isinstance(value, str | bool | int):
        return value
    return str(value)


def _srid_of(crs: Any) -> int | None:
    """`"EPSG:32642"` -> `32642`. None when the CRS is absent or in a form we
    refuse to interpret (a bare WKT string, a non-EPSG authority) — the caller
    turns that into a `no_crs`/`unsupported_crs` error row."""
    if not crs:
        return None
    text = str(crs).strip()
    if text in _CRS84:
        return 4326
    match = _EPSG.match(text)
    return int(match.group(1)) if match else None


def parse(data: bytes, *, fmt: str) -> tuple[list[ParsedFeature], list[RowError], int]:
    """Read one uploaded geodata file. Returns `(features, errors, srid)`.

    Every GDAL failure becomes a `RowError`, never an exception: this runs
    inside a background job whose whole purpose is to turn a bad delivery into a
    readable report. A feature whose geometry is NULL becomes its own row error
    and parsing CONTINUES, so the report lists every bad row rather than only
    the first — an operator fixing a 151-row file must see all of it at once
    (tz/07: "error report — row number plus error type").
    """
    suffix = _SUFFIX.get(fmt)
    if suffix is None:
        return (
            [],
            [RowError(row=0, code="unsupported_format", message=f"unsupported format: {fmt!r}")],
            _NO_SRID,
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"upload{suffix}"
        path.write_bytes(data)
        source = f"/vsizip/{path}" if suffix == ".zip" else str(path)
        try:
            info = pyogrio.read_info(source)
        except Exception as exc:  # every GDAL failure — a truncated zip, a missing .dbf
            # repr(), not str(): an empty message would otherwise leave the
            # report saying nothing at all (lesson).
            return [], [RowError(row=0, code="unreadable_file", message=repr(exc))], _NO_SRID
        if not info.get("crs"):
            return [], [RowError(row=0, code="no_crs", message="source has no CRS")], _NO_SRID
        srid = _srid_of(info["crs"])
        if srid is None:
            return (
                [],
                [
                    RowError(
                        row=0,
                        code="unsupported_crs",
                        message=f"CRS is not an EPSG code: {info['crs']!r}",
                    )
                ],
                _NO_SRID,
            )

        try:
            meta, _indices, geometry, field_data = pyogrio.raw.read(
                source, force_2d=True, max_features=None
            )
        except Exception as exc:
            return [], [RowError(row=0, code="unreadable_file", message=repr(exc))], _NO_SRID

        fields = [str(name) for name in meta["fields"]]
        features: list[ParsedFeature] = []
        errors: list[RowError] = []
        if geometry is None:  # an attribute-only source (a CSV with no geometry column)
            return (
                [],
                [RowError(row=0, code="no_geometry", message="source carries no geometry")],
                _NO_SRID,
            )
        # Paired once, not per row: `meta["fields"]` and `field_data` are two
        # halves of one structure and must line up (strict=True says so out loud).
        columns = list(zip(fields, field_data, strict=True))
        omitted = 0
        for i, wkb in enumerate(geometry):
            if wkb is None or len(wkb) == 0:
                # Counted past the cap, not accumulated: the attributes dict is
                # not even built for a row that will never be reported, so the
                # memory a million-row disaster costs is bounded too, not just
                # the column it would be written to.
                if len(errors) < MAX_REPORT_ROWS:
                    errors.append(
                        RowError(row=i, code="empty_geometry", message="feature has no geometry")
                    )
                else:
                    omitted += 1
                continue
            attributes = {name: _scalar(column[i]) for name, column in columns}
            features.append(ParsedFeature(row=i, wkb=bytes(wkb), attributes=attributes))
        if omitted:
            errors.append(truncation_marker(omitted))
        return features, errors, srid
