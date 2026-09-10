"""Fixtures for `occupancy`. Spatial and permit primitives are imported as
plain importables from `gis`'s and `permits`' own test packages — the same
idiom `tests/modules/norms/conftest.py` and `tests/modules/dashboard/
conftest.py` already use, so there is one way to build a published contour
and one way to build a permit, not a second copy drifting from either.

`_app_on_test_db` is required in EVERY HTTP-tested package (lesson) — it does
not propagate from a sibling package's conftest."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.models import MediaFile
from app.modules.admin.models import Organization
from app.modules.auth.models import User
from app.modules.gis.models import Contour, ContourVersion, GisLayer
from app.modules.norms.models import Norm
from tests.modules.gis.conftest import applicant_client as applicant_client  # noqa: F401
from tests.modules.gis.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.gis.conftest import gis_user as gis_user  # noqa: F401
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import count_queries as count_queries  # noqa: F401
from tests.modules.permits.conftest import (
    grazing_activity_id as grazing_activity_id,  # noqa: F401
)
from tests.modules.permits.conftest import (
    haymaking_activity_id as haymaking_activity_id,  # noqa: F401
)
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz, approval_doc: MediaFile
) -> Contour:
    """A published contour at a random, isolated spot — a fixed committed
    geometry accumulates across runs on the shared test DB (lesson).
    `approval_doc_id` is mandatory: `ck_contour_versions_published_needs_doc`
    rejects a published version without one."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    return contour


async def published_version_id(db: AsyncSession, contour: Contour) -> uuid.UUID:
    return (
        await db.execute(select(ContourVersion.id).where(ContourVersion.contour_id == contour.id))
    ).scalar_one()


async def make_published_norm(
    db: AsyncSession,
    *,
    contour: Contour,
    activity_type_id: uuid.UUID,
    gis_user: User,
    approval_doc: MediaFile,
    max_sb: int | None = None,
    capacity: Decimal | None = None,
) -> Norm:
    """A published norm, inserted directly (ORM) the way `norms.conftest.
    published_grazing_norm` does — publishing through the API needs the whole
    approve -> publish chain, another module's own territory. `max_sb`/
    `capacity` are the two capacity numbers ruling #176 generalised between;
    both `None` is a norm that carries no capacity at all — the EXCLUSIVE
    case, on purpose, for a test that wants one."""
    norm = Norm(
        contour_id=contour.id,
        activity_type_id=activity_type_id,
        max_sb=max_sb,
        capacity=capacity,
        effective_from=date(2020, 1, 1),
        status="published",
        approval_doc_id=approval_doc.id,
        created_by=gis_user.id,
        approved_by=gis_user.id,
    )
    db.add(norm)
    await db.flush()
    return norm


def occupancy_url(contour_id: uuid.UUID, activity_type_id: uuid.UUID, from_: date, to: date) -> str:
    return (
        f"{API}/gis/contours/{contour_id}/occupancy"
        f"?activity_type_id={activity_type_id}&from={from_.isoformat()}&to={to.isoformat()}"
    )


async def issue_permit(
    db: AsyncSession,
    *,
    contour: Contour,
    org: Organization,
    activity_type_id: uuid.UUID,
    period_from: date,
    period_to: date,
    sb_load: Decimal | None,
    quantity: Decimal | None = None,
    status: str = "active",
):
    """A permit on `contour`'s own published version — a thin wrapper over
    `permits.conftest.make_permit_on_contour` naming only what these tests
    vary."""
    version_id = await published_version_id(db, contour)
    return await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=org,
        activity_type_id=activity_type_id,
        status=status,
        sb_load=sb_load,
        quantity=quantity,
        period_from=period_from,
        period_to=period_to,
    )
