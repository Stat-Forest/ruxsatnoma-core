"""CLI: load one leshoz's contour layer from the Agency's KMZ delivery of 2026-09-13.

    uv run python -m app.seed.agency_kmz "<path>/<Leshoz>_kontur.kmz" --org burchmulla
    uv run python -m app.seed.agency_kmz "<...>.kmz" --dry-run --out contours.geojson

The delivery (`data/geodata/all-leshozes/README.md`) is an ArcGIS "Layer To KML"
export whose attributes live in an HTML table inside `<description>`, NOT in
`<ExtendedData>` — so `gis.importer.parse` (GDAL) sees the geometry and no fields
at all. This module rewrites one KMZ into a GeoJSON FeatureCollection with the
attributes GDAL can read, then pushes it through `gis`'s own import pipeline
exactly the way `app.seed.demo` does for the lease layer: create -> parse ->
submit-review -> approve -> publish. Never a raw `contours` insert.

Seven contour schemas exist across the 70 files (README, "Contour layer"). The
contour number is taken from the first present of `Kontur raqami`, `yangi_k_r`,
`eski_k_r`; the declared area from `Umumiy yer maydoni`, `F12`, `umum_y_m`, and
failing all three from `SHAPE_Area` (m² -> ha). Every raw field is kept in the
GeoJSON properties (comma decimals turned into floats, `<Null>` into null) so a
`--dry-run --out` file is a complete conversion, not only the two mapped keys.

Rows with no number at all, rows whose coordinates fall outside Uzbekistan
(Qiziriq's land-type file carries 18 legend polygons in the Indian Ocean), and
rows whose polygon has no area (Zomin, Ellikqal'a, Sirdaryo and Bobotog each
carry one collapsed three-point ring — `ck_contour_versions_area_positive`
refuses it and the import is atomic, so one such row failed the whole leshoz)
are dropped here and counted in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import files as core_files
from app.db import make_engine, make_session_factory
from app.modules.admin import repo as admin_repo
from app.modules.auth.models import User
from app.modules.gis import import_service as gis_import_service
from app.modules.gis import service as gis_service

KML_NS = "{http://www.opengis.net/kml/2.2}"

NUMBER_FIELDS = ("Kontur raqami", "yangi_k_r", "eski_k_r")
AREA_HA_FIELDS = ("Umumiy yer maydoni", "F12", "umum_y_m")
# Uzbekistan's bounding box with a margin; anything outside is a template polygon.
LON_RANGE = (55.0, 74.0)
LAT_RANGE = (36.0, 46.0)
# Planar shoelace area in square degrees below which a ring is a collapsed line:
# 1e-10 deg² is about 1 m² at this latitude.
MIN_RING_AREA_DEG2 = 1e-10

_ROW_RE = re.compile(r"<tr[^>]*>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", re.S)
_NUMBER_RE = re.compile(r"^-?\d+(,\d+)?$")

_MIN_PDF = b"%PDF-1.4\n%agency geodata delivery 2026-09-13 - placeholder approval\n%%EOF\n"


def _value(raw: str) -> Any:
    text = html.unescape(raw).strip()
    if text in ("", "<Null>"):
        return None
    if _NUMBER_RE.match(text):
        return float(text.replace(",", ".")) if "," in text else int(text)
    return text


def _attributes(description: str) -> dict[str, Any]:
    """The HTML table: `<tr><td>name</td><td>value</td></tr>` per field. The
    first row is the header cell (the contour number as a title) whose "name"
    swallows the nested table's opening tags — skip anything with markup in
    the name, and the `SHAPE` row, which only repeats the geometry type."""
    out: dict[str, Any] = {}
    for name, value in _ROW_RE.findall(description):
        if "<" in name or name.strip() == "SHAPE":
            continue
        out[html.unescape(name).strip()] = _value(value)
    return out


def _ring(elem: ET.Element) -> list[list[float]] | None:
    coords = elem.find(f"{KML_NS}coordinates")
    if coords is None or not coords.text:
        return None
    ring = []
    for token in coords.text.split():
        lon, lat = (float(v) for v in token.split(",")[:2])
        ring.append([lon, lat])
    return ring if len(ring) >= 4 else None


def _polygons(placemark: ET.Element) -> list[list[list[list[float]]]]:
    polys = []
    for poly in placemark.iter(f"{KML_NS}Polygon"):
        outer = poly.find(f"{KML_NS}outerBoundaryIs/{KML_NS}LinearRing")
        rings = [_ring(outer)] if outer is not None else []
        for inner in poly.findall(f"{KML_NS}innerBoundaryIs/{KML_NS}LinearRing"):
            rings.append(_ring(inner))
        rings = [r for r in rings if r]
        if rings:
            polys.append(rings)
    return polys


def _in_uzbekistan(polys: list[list[list[list[float]]]]) -> bool:
    for rings in polys:
        for ring in rings:
            for lon, lat in ring:
                lon_ok = LON_RANGE[0] <= lon <= LON_RANGE[1]
                lat_ok = LAT_RANGE[0] <= lat <= LAT_RANGE[1]
                if not (lon_ok and lat_ok):
                    return False
    return True


def _has_area(polys: list[list[list[list[float]]]]) -> bool:
    """True when at least one outer ring encloses a real area (shoelace)."""
    for rings in polys:
        outer = rings[0]
        twice = 0.0
        for (x1, y1), (x2, y2) in zip(outer, outer[1:] + outer[:1], strict=True):
            twice += x1 * y2 - x2 * y1
        if abs(twice) / 2 >= MIN_RING_AREA_DEG2:
            return True
    return False


def _first(attrs: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if attrs.get(name) is not None:
            return attrs[name]
    return None


def convert(kmz_path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    """One KMZ -> (GeoJSON FeatureCollection, counters)."""
    with zipfile.ZipFile(kmz_path) as zf:
        kml_name = next(n for n in zf.namelist() if n.endswith(".kml"))
        # Operator-run CLI over the Agency's own delivery, never a network
        # upload — a billion-laughs payload here would only hang the operator.
        root = ET.fromstring(zf.read(kml_name))  # nosec B314

    features: list[dict[str, Any]] = []
    stats = {
        "placemarks": 0,
        "no_number": 0,
        "no_geometry": 0,
        "outside_uz": 0,
        "zero_area": 0,
        "kept": 0,
    }
    for pm in root.iter(f"{KML_NS}Placemark"):
        stats["placemarks"] += 1
        desc = pm.find(f"{KML_NS}description")
        attrs = _attributes(desc.text or "") if desc is not None else {}
        number = _first(attrs, NUMBER_FIELDS)
        if number is None:
            stats["no_number"] += 1
            continue
        polys = _polygons(pm)
        if not polys:
            stats["no_geometry"] += 1
            continue
        if not _in_uzbekistan(polys):
            stats["outside_uz"] += 1
            continue
        if not _has_area(polys):
            stats["zero_area"] += 1
            continue
        area_ha = _first(attrs, AREA_HA_FIELDS)
        if area_ha is None and isinstance(attrs.get("SHAPE_Area"), (int, float)):
            area_ha = round(attrs["SHAPE_Area"] / 10_000, 4)
        props = {
            "number": str(number),
            "declared_area_ha": area_ha,
            "new_number": attrs.get("yangi_k_r") or attrs.get("Yangi_kontur"),
            "placemark_id": pm.get("id"),
            **attrs,
        }
        geometry = (
            {"type": "Polygon", "coordinates": polys[0]}
            if len(polys) == 1
            else {"type": "MultiPolygon", "coordinates": polys}
        )
        features.append({"type": "Feature", "geometry": geometry, "properties": props})
        stats["kept"] += 1
    return {"type": "FeatureCollection", "features": features}, stats


async def _load(
    db: AsyncSession, *, geojson: bytes, org_code: str, actor_login: str, filename: str
) -> str:
    org = await admin_repo.get_organization_by_code(db, org_code)
    if org is None:
        return f"ERROR: organization {org_code!r} not found"
    actor = (await db.execute(select(User).where(User.login == actor_login))).scalar_one_or_none()
    if actor is None:
        return f"ERROR: actor {actor_login!r} not found"

    approval_doc = await core_files.save_upload(
        db,
        data=_MIN_PDF,
        filename=f"{org_code}-agency-kmz-approval.pdf",
        content_type="application/pdf",
        actor=actor,
    )
    row = await gis_import_service.create_import(
        db,
        layer_code="contours",
        organization_id=org.id,
        approval_doc_id=approval_doc.id,
        fmt="geojson",
        attribute_map={"number": "number", "declared_area_ha": "declared_area_ha"},
        data=geojson,
        filename=filename,
        content_type="application/geo+json",
        actor=actor,
    )
    await gis_import_service.run_import(db, row)
    if row.status == "failed":
        report = json.dumps(row.error_report, ensure_ascii=False)[:2000]
        return f"FAILED: import {row.id} — {report}"
    await gis_service.submit_import_review(db, row.id, actor=actor)
    await gis_service.approve_import(db, row.id, actor=actor)
    result = await gis_service.publish_import(db, row.id, actor=actor)
    stats = row.stats or {}
    warnings = stats.get("warnings") or []
    msg = (
        f"import {row.id}: created={stats.get('created')} published={result['published']} "
        f"blocked={len(result['blocked'])} warnings={len(warnings)}"
    )
    if result["blocked"]:
        sample = json.dumps(result["blocked"][:3], ensure_ascii=False, default=str)
        msg += f"\nblocked sample: {sample}"
    return msg


async def _main(args: argparse.Namespace) -> None:
    collection, stats = await asyncio.to_thread(convert, args.kmz)
    print(f"{args.kmz.name}: {stats}")
    payload = json.dumps(collection, ensure_ascii=False).encode()
    print(f"geojson: {len(payload) / 1_048_576:.1f} MB, {len(collection['features'])} features")
    if args.out:
        args.out.write_bytes(payload)
        print(f"written to {args.out}")
    if args.dry_run:
        return
    if not args.org:
        raise SystemExit("--org <organization code> is required unless --dry-run")
    engine = make_engine(get_settings().database_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as db:
            report = await _load(
                db,
                geojson=payload,
                org_code=args.org,
                actor_login=args.actor,
                filename=args.kmz.stem + ".geojson",
            )
            await db.commit()
        print(report)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.seed.agency_kmz")
    parser.add_argument("kmz", type=Path)
    parser.add_argument("--org", help="organization code (e.g. burchmulla)")
    parser.add_argument("--actor", default="demo_sysadmin", help="login that runs the pipeline")
    parser.add_argument("--dry-run", action="store_true", help="convert only, do not touch the DB")
    parser.add_argument("--out", type=Path, help="also write the GeoJSON here")
    args = parser.parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
