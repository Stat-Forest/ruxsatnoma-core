"""RI-03 (plan ruling a): the one code this module detects directly rather
than harvests. Neither fixture publishes its contour version — this sweep
reads only `permits` (`repo.overlapping_active_permit_pairs`), the same
reasoning `tests/modules/permits/test_jobs.py`'s own `contour` fixture gives
for staying unpublished."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, GisLayer
from app.modules.oversight import service
from app.modules.oversight.models import RiskIndicator
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour


@pytest.fixture
async def contour(db: AsyncSession, contours_layer: GisLayer, leshoz: Organization) -> Contour:
    row = await make_contour(db, contours_layer, leshoz)
    await make_version(db, row.id, random_box_wkt())
    return row


@pytest.fixture
async def version_id(db: AsyncSession, contour: Contour):
    from app.modules.gis.models import ContourVersion

    return (
        await db.execute(select(ContourVersion.id).where(ContourVersion.contour_id == contour.id))
    ).scalar_one()


async def _ri03_on(db: AsyncSession, contour_id) -> list[RiskIndicator]:
    """The RI-03 rows about permits on THIS test's contour.

    Everything here filters on the fixture's own contour because
    `sweep_overlapping_permits` is a whole-table scan: the number it returns
    counts every overlapping pair in the DATABASE, not this test's. Under
    `pytest -n 4` (what CI runs) the other suites have committed permits of
    their own by the time this file runs, so asserting on that global number
    made the test pass or fail on which worker got there first — three of
    these three failed that way on PR #75 while passing locally and on the
    run before it."""
    rows = (
        await db.execute(
            select(RiskIndicator).where(
                RiskIndicator.code == "RI-03",
                RiskIndicator.details["contour_id"].astext == str(contour_id),
            )
        )
    ).scalars()
    return list(rows)


async def test_raises_ri03_for_two_active_overlapping_permits(
    db, contour, version_id, leshoz, grazing_activity_id
):
    permit_a = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_from=date(2027, 5, 1),
        period_to=date(2027, 7, 1),
    )
    permit_b = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="suspended",
        period_from=date(2027, 6, 1),
        period_to=date(2027, 8, 1),
    )

    await service.sweep_overlapping_permits(db)

    mine = await _ri03_on(db, contour.id)
    assert len(mine) == 1
    row = mine[0]
    assert row.level == "high"
    assert row.object_type == "permit"
    details = row.details or {}
    assert {details["permit_a"], details["permit_b"]} == {
        str(permit_a.id),
        str(permit_b.id),
    }

    # Idempotent: a second run over the same pair raises nothing new.
    await service.sweep_overlapping_permits(db)
    assert len(await _ri03_on(db, contour.id)) == 1


async def test_ignores_non_overlapping_periods(
    db, contour, version_id, leshoz, grazing_activity_id
):
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_from=date(2027, 1, 1),
        period_to=date(2027, 2, 1),
    )
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_from=date(2027, 3, 1),
        period_to=date(2027, 4, 1),
    )

    await service.sweep_overlapping_permits(db)

    assert await _ri03_on(db, contour.id) == []


async def test_ignores_a_revoked_permit(db, contour, version_id, leshoz, grazing_activity_id):
    """`revoked` is deliberately excluded — an operator already dealt with it
    (`payments.repo._RI_10_PERMIT_STATUSES`'s own reasoning applies here too:
    an RI on it would be a false positive)."""
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="revoked",
        period_from=date(2027, 5, 1),
        period_to=date(2027, 7, 1),
    )
    await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
        period_from=date(2027, 6, 1),
        period_to=date(2027, 8, 1),
    )

    await service.sweep_overlapping_permits(db)

    assert await _ri03_on(db, contour.id) == []
