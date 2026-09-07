"""RI-14 (ruling #104): the second direct detector this module raises rather
than harvests — no existing `audit_log` tag anywhere names "long active with
no inspection", the same reasoning `test_overlap_sweep.py` gives for RI-03.

Every permit here is built through `permits.conftest.make_permit_on_contour`
(a read-only import — `oversight` never writes `permits`' tables) with its
`issued_at` set by hand afterward: that fixture leaves the column `NULL`,
which is correct for its OWN tests (RI-03 never reads it) but useless for
this file, whose whole subject is that one column's age."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.models import SystemSetting
from app.modules.admin.models import Organization
from app.modules.gis.models import Contour, GisLayer
from app.modules.inspections.models import InspectionAct
from app.modules.oversight import service
from app.modules.oversight.models import RiskIndicator
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.inspections.conftest import (
    default_checklist_id as default_checklist_id,  # noqa: F401
)
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour

THRESHOLD_SETTING = service.RI14_THRESHOLD_SETTING


@pytest.fixture(autouse=True)
def _clear_setting_cache():
    """The RI-14 threshold test overrides the setting mid-test — the same
    guard `tests/core/test_settings_store.py` uses, so a value another test
    in this worker cached within `settings_store`'s own 60s TTL never leaks
    in, and this file never leaks its own override back out."""
    settings_store.invalidate(THRESHOLD_SETTING)
    yield
    settings_store.invalidate(THRESHOLD_SETTING)


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


async def _make_active_permit(db, *, contour, version_id, leshoz, grazing_activity_id, age_days):
    """An `active` permit whose `issued_at` is `age_days` in the past —
    `make_permit_on_contour` never sets that column (RI-03 does not read it),
    so it is set here and flushed a second time."""
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
    )
    permit.issued_at = datetime.now(UTC) - timedelta(days=age_days)
    await db.flush()
    return permit


async def _ri14_rows(db: AsyncSession, permit_id) -> list[RiskIndicator]:
    stmt = select(RiskIndicator).where(
        RiskIndicator.code == "RI-14", RiskIndicator.object_id == permit_id
    )
    return list((await db.execute(stmt)).scalars().all())


async def test_raises_ri14_once_for_a_31_day_old_permit_with_no_act(
    db, contour, version_id, leshoz, grazing_activity_id
):
    permit = await _make_active_permit(
        db,
        contour=contour,
        version_id=version_id,
        leshoz=leshoz,
        grazing_activity_id=grazing_activity_id,
        age_days=31,
    )

    written = await service.sweep_long_active_without_inspection(db)

    assert written == 1
    rows = await _ri14_rows(db, permit.id)
    assert len(rows) == 1
    assert rows[0].level == "low"
    assert rows[0].object_type == "permit"
    assert rows[0].details is not None
    assert rows[0].details["threshold_days"] == 30

    # Idempotent: a second sweep over the SAME still-unvisited permit raises
    # nothing new — ruling #104's own "fires once, not on every pass".
    assert await service.sweep_long_active_without_inspection(db) == 0
    assert len(await _ri14_rows(db, permit.id)) == 1


async def test_does_not_raise_before_the_threshold(
    db, contour, version_id, leshoz, grazing_activity_id
):
    await _make_active_permit(
        db,
        contour=contour,
        version_id=version_id,
        leshoz=leshoz,
        grazing_activity_id=grazing_activity_id,
        age_days=10,
    )

    assert await service.sweep_long_active_without_inspection(db) == 0


async def test_an_act_on_the_permit_suppresses_ri14(
    db,
    contour,
    version_id,
    leshoz,
    grazing_activity_id,
    default_checklist_id,
):
    permit = await _make_active_permit(
        db,
        contour=contour,
        version_id=version_id,
        leshoz=leshoz,
        grazing_activity_id=grazing_activity_id,
        age_days=45,
    )
    inspector = await make_user(db, role_code="inspector", organization_id=leshoz.id)
    db.add(
        InspectionAct(
            permit_id=permit.id,
            organization_id=leshoz.id,
            inspector_id=inspector.id,
            occurred_at=datetime.now(UTC),
            checklist_id=default_checklist_id,
        )
    )
    await db.flush()

    assert await service.sweep_long_active_without_inspection(db) == 0
    assert await _ri14_rows(db, permit.id) == []


async def test_threshold_is_read_from_settings(
    db, contour, version_id, leshoz, grazing_activity_id
):
    """A permit that the default 30-day threshold would ignore fires once the
    setting is tightened to 5 — pinning that the sweep reads `system_settings`
    fresh rather than a frozen module constant."""
    permit = await _make_active_permit(
        db,
        contour=contour,
        version_id=version_id,
        leshoz=leshoz,
        grazing_activity_id=grazing_activity_id,
        age_days=10,
    )
    assert await service.sweep_long_active_without_inspection(db) == 0

    db.add(SystemSetting(key=THRESHOLD_SETTING, value=5))
    await db.flush()
    settings_store.invalidate(THRESHOLD_SETTING)

    written = await service.sweep_long_active_without_inspection(db)

    assert written == 1
    rows = await _ri14_rows(db, permit.id)
    assert len(rows) == 1
    assert rows[0].details is not None
    assert rows[0].details["threshold_days"] == 5
