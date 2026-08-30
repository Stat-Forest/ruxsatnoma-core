"""Schema-level guarantees of the gis tables: the 15 seeded layers, exactly one
published version per contour, the approval-document CHECK that ruling 3 keeps
intact, and the contour-number uniqueness that ruling 11's suffixing depends on."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db import uuid7
from app.modules.gis.models import LAYER_CODES, Contour, ContourVersion, GisLayer
from tests.modules.gis.conftest import box_wkt, make_contour


async def _version(db, contour, status: str, **over):
    """Insert a version with real geometry (SQLAlchemy cannot bind WKT directly)."""
    fields = {
        "contour_id": contour.id,
        "version_no": over.pop("version_no", 1),
        "geom": func.ST_Multi(func.ST_GeomFromText(box_wkt(69.9, 41.5), 4326)),
        "area_ha": 92.0,
        "source": "survey",
        "status": status,
    }
    fields.update(over)
    version = ContourVersion(**fields)
    db.add(version)
    await db.flush()
    return version


async def test_fifteen_layers_are_seeded(db):
    codes = set((await db.execute(select(GisLayer.code))).scalars().all())
    assert set(LAYER_CODES) <= codes
    assert len(LAYER_CODES) == 15


async def test_only_one_published_version_per_contour(db, contours_layer, leshoz, approval_doc):
    """A published row needs a document (the CHECK below), so both rows carry one —
    what this test isolates is the partial unique index, nothing else."""
    contour = await make_contour(db, contours_layer, leshoz)
    await _version(
        db,
        contour,
        "published",
        version_no=1,
        approval_doc_id=approval_doc.id,
        published_at=func.now(),
    )
    with pytest.raises(IntegrityError):
        await _version(
            db,
            contour,
            "published",
            version_no=2,
            approval_doc_id=approval_doc.id,
            published_at=func.now(),
        )


async def test_published_version_requires_an_approval_document(db, contours_layer, leshoz):
    """Ruling 3 keeps the CHECK: a batch shares ONE document, it does not skip it."""
    contour = await make_contour(db, contours_layer, leshoz)
    with pytest.raises(IntegrityError) as excinfo:
        await db.execute(
            text(
                "INSERT INTO contour_versions (id, contour_id, version_no, geom, area_ha,"
                " source, status, approval_doc_id)"
                " VALUES (gen_random_uuid(), :cid, 1,"
                " ST_Multi(ST_GeomFromText(:wkt, 4326)), 1.0, 'survey', 'published', NULL)"
            ),
            {"cid": contour.id, "wkt": box_wkt(69.9, 41.5)},
        )
    assert "published_needs_doc" in str(excinfo.value)


async def test_contour_number_is_unique_per_organization(db, contours_layer, leshoz):
    _contour = await make_contour(db, contours_layer, leshoz, number="14515q")
    with pytest.raises(IntegrityError):
        await make_contour(db, contours_layer, leshoz, number="14515q")


async def test_parent_needs_subcontour_check_fires(db, contours_layer, leshoz):
    """The parent_needs_subcontour CHECK itself, at the model/DB level.
    gis.service.create_contour pre-validates this same rule in Python and never
    reaches the constraint (see
    test_contours_api.py::test_a_parent_id_requires_kind_subcontour for that
    guard) — this proves the DB-level backstop holds on its own, independent of
    that guard (final review, finding 2). `parent_id` does not need to
    reference a real row: the CHECK fires on kind/parent_id alone, before any
    FK lookup."""
    contour = Contour(
        layer_id=contours_layer.id,
        organization_id=leshoz.id,
        kind="contour",
        parent_id=uuid7(),
        number="parent-needs-subcontour-check",
    )
    db.add(contour)
    with pytest.raises(IntegrityError) as excinfo:
        await db.flush()
    assert "parent_needs_subcontour" in str(excinfo.value)
