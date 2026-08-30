"""The importer reads what the Agency actually sends. Fixtures are generated with
pyogrio.raw.write + shapely, so these are real shapefiles and real GeoPackages,
not strings that resemble them."""

import json
import warnings
import zipfile

import numpy as np
import shapely
from pyogrio.raw import write

from app.modules.gis import importer


def _shapefile_zip(tmp_path, *, rows: int = 2, crs: str | None = "EPSG:4326") -> bytes:
    poly = shapely.from_wkt("POLYGON((69.9 41.5, 69.91 41.5, 69.91 41.51, 69.9 41.51, 69.9 41.5))")
    with warnings.catch_warnings():
        # crs=None is the point of one of the tests below; pyogrio warns about it,
        # which is correct of it and noise here.
        warnings.simplefilter("ignore", UserWarning)
        write(
            str(tmp_path / "layer.shp"),
            geometry=shapely.to_wkb(np.array([poly] * rows)),
            field_data=[
                np.array([f"1451{i}q" for i in range(rows)], dtype=object),
                np.array([2.6] * rows),
            ],
            fields=np.array(["number", "area_ha"], dtype=object),
            geometry_type="Polygon",
            crs=crs,
            driver="ESRI Shapefile",
            encoding="UTF-8",
        )
    buffer = tmp_path / "layer.zip"
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in tmp_path.glob("layer.*"):
            if path.suffix != ".zip":
                archive.write(path, path.name)
    return buffer.read_bytes()


def test_a_zipped_shapefile_is_read_with_its_attributes(tmp_path):
    features, errors, srid = importer.parse(_shapefile_zip(tmp_path), fmt="shp")
    assert errors == []
    assert len(features) == 2
    assert srid == 4326
    assert features[0].wkb[:1] in {b"\x00", b"\x01"}  # WKB endianness byte
    assert features[0].attributes["number"] == "14510q"


def test_a_bare_shp_without_its_sidecars_is_an_error(tmp_path):
    """Ruling 5: the attributes live in the .dbf, the CRS in the .prj."""
    features, errors, _ = importer.parse(b"\x00\x00\x27\x0a" + b"\x00" * 96, fmt="shp")
    assert features == []
    assert errors[0].code == "unreadable_file"


def test_geojson_is_read_directly():
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[69.9, 41.5], [69.91, 41.5], [69.91, 41.51], [69.9, 41.5]]
                        ],
                    },
                    "properties": {"number": "14515q"},
                }
            ],
        }
    ).encode()
    features, errors, _ = importer.parse(payload, fmt="geojson")
    assert errors == []
    assert features[0].attributes["number"] == "14515q"


def test_a_source_without_a_crs_is_rejected_not_assumed(tmp_path):
    """Assuming 4326 for an unprojected file would put a leshoz in the ocean."""
    features, errors, _ = importer.parse(_shapefile_zip(tmp_path, crs=None), fmt="shp")
    assert features == []
    assert errors[0].code == "no_crs"


def test_a_projected_source_reports_its_srid_for_postgis_to_transform(tmp_path):
    """Decision #13: other projections are reprojected at import — by PostGIS, so
    the parser's job is to report the source SRID faithfully, not to transform."""
    features, errors, srid = importer.parse(_shapefile_zip(tmp_path, crs="EPSG:32642"), fmt="shp")
    assert errors == []
    assert srid == 32642


def test_garbage_is_an_error_not_an_exception():
    features, errors, _ = importer.parse(b"not a map at all", fmt="geojson")
    assert features == []
    assert errors[0].code == "unreadable_file"


# --- Beyond the brief's own cases -------------------------------------------


def test_a_feature_without_geometry_is_a_row_error_and_the_rest_still_parse():
    """tz/07's error report is "row number plus error type" for EVERY bad row —
    an operator fixing a 151-row file must see all of it at once, so parsing
    continues past the first failure instead of returning on it."""
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": None,
                    "properties": {"number": "empty"},
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[69.9, 41.5], [69.91, 41.5], [69.91, 41.51], [69.9, 41.5]]
                        ],
                    },
                    "properties": {"number": "good"},
                },
            ],
        }
    ).encode()
    features, errors, _ = importer.parse(payload, fmt="geojson")
    assert [(e.row, e.code) for e in errors] == [(0, "empty_geometry")]
    assert [f.attributes["number"] for f in features] == ["good"]


def test_numpy_scalars_are_coerced_to_json_primitives():
    """`pyogrio` hands back numpy columns, and every attribute ends up in a JSONB
    bind (`layer_features.props`, `gis_imports.stats`) served by the stock
    `json.dumps` with no encoder configured — the same class of failure as the
    `Decimal` one in `.claude/lessons.md`. A NULL numeric arrives as NaN, which
    `json.dumps` renders as the literal `NaN`: valid Python, invalid JSON, and
    rejected outright by a `jsonb` column."""
    payload = json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [69.9, 41.5]},
                    "properties": {"count": 3, "area": 2.5, "flag": True, "missing": None},
                }
            ],
        }
    ).encode()
    features, errors, _ = importer.parse(payload, fmt="geojson")
    assert errors == []
    attributes = features[0].attributes
    assert {type(v) for v in attributes.values()} <= {int, float, bool, str, type(None)}
    assert attributes["count"] == 3
    assert attributes["area"] == 2.5
    assert attributes["flag"] is True
    assert attributes["missing"] is None
    json.dumps(attributes)  # the actual invariant: it survives a JSONB bind


def test_an_unknown_format_is_rejected_before_gdal_sees_it():
    features, errors, _ = importer.parse(b"PK\x03\x04", fmt="rar")
    assert features == []
    assert errors[0].code == "unsupported_format"
