"""Shared gis fixtures. Geometry is written as WKT and converted by PostGIS, so a
test never hand-builds GeoJSON; areas are real (a 0.01° x 0.01° box near Tashkent
is roughly 92 ha), which is what makes the area assertions meaningful."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.db import uuid7
from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, GisLayer


def box_wkt(min_lon: float, min_lat: float, size: float = 0.01) -> str:
    """A closed square polygon in WGS84 degrees."""
    x0, y0, x1, y1 = min_lon, min_lat, min_lon + size, min_lat + size
    return f"POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"


@pytest.fixture
async def contours_layer(db: AsyncSession) -> GisLayer:
    layer = (await db.execute(select(GisLayer).where(GisLayer.code == "contours"))).scalar_one()
    return layer


@pytest.fixture
async def leshoz(db: AsyncSession) -> Organization:
    """An organization of our own, so tests never collide on the shared test DB.

    `ck_organizations_root_is_agency` requires a non-agency row to carry a
    parent, and the single-agency partial unique index makes the agency row a
    singleton another test module may already have committed to the shared,
    persistent test DB — so this reuses one if it exists rather than assuming
    a fresh database (lesson: "the test database is shared, persistent").
    """
    agency = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if agency is None:
        agency = Organization(
            id=uuid7(),
            code=f"A{uuid.uuid4().hex[:8]}",
            name={"uz_cyrl": "Тест агентлиги", "ru": "Тестовое агентство"},
            kind="agency",
        )
        db.add(agency)
        await db.flush()
    org = Organization(
        id=uuid7(),
        code=f"T{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Тест ЎХ", "ru": "Тестовый лесхоз"},
        kind="leshoz",
        parent_id=agency.id,
    )
    db.add(org)
    await db.flush()
    return org


@pytest.fixture
async def approval_doc(db: AsyncSession) -> MediaFile:
    """An active media_files row standing in for a basis document (decree, act) —
    what a published contour version's approval_doc_id CHECK requires (ruling 3)."""
    doc = MediaFile(
        storage_key=f"t/{uuid.uuid4().hex}",
        filename="approval.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="0" * 64,
    )
    db.add(doc)
    await db.flush()
    return doc


async def make_contour(
    db: AsyncSession, layer: GisLayer, org: Organization, **over: Any
) -> Contour:
    fields: dict[str, Any] = {
        "id": uuid7(),
        "layer_id": layer.id,
        "organization_id": org.id,
        "number": f"C{uuid.uuid4().hex[:8]}",
        "kind": "contour",
        "status": "active",
    }
    fields.update(over)
    contour = Contour(**fields)
    db.add(contour)
    await db.flush()
    return contour
