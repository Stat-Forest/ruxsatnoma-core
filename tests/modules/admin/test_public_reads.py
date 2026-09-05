"""`admin.service.list_regions`/`list_organizations` — thin pass-throughs added
for 4.6 `public`'s open-data aggregate, which may reach `admin` only through
its service (design/01 rule 2)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin import service
from app.modules.admin.models import Organization, Region
from tests.modules.gis.conftest import leshoz as leshoz


async def test_list_regions_returns_a_region_we_created(db: AsyncSession):
    """`flush`, not `commit`: `list_regions` reads through this SAME session, so
    autoflush already makes the row visible to it, and `regions` is reference
    data seeded once and read everywhere (`test_refs_api.py::test_regions_list`
    counts all fourteen) — a `commit` here would outlive the `db` fixture's own
    rollback and leave a fifteenth row in the shared, persistent test database
    forever."""
    region = Region(code=f"r-{uuid.uuid4().hex[:8]}", name={"uz_cyrl": "Тест вилояти"})
    db.add(region)
    await db.flush()

    regions = await service.list_regions(db)
    assert any(r.id == region.id for r in regions)


async def test_list_organizations_filters_by_kind(db: AsyncSession, leshoz: Organization):
    orgs = await service.list_organizations(db, kind="leshoz")
    assert any(o.id == leshoz.id for o in orgs)
    assert all(o.kind == "leshoz" for o in orgs)
