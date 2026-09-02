import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.permits.models import Permit, PermitStatusHistory


async def _permit(db, application, *, series="А", number=1, status="pending_signatures") -> Permit:
    row = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=application.applicant_id,
        activity_type_id=application.activity_type_id,
        organization_id=application.assigned_org_id,
        contour_id=application.contour_id,
        contour_version_id=application.contour_version_id,
        area_ha=Decimal("12.5000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        amount=Decimal("2060000.00"),
        sb_load=Decimal("40.0000"),
        status=status,
        qr_token=f"tok-{uuid.uuid4().hex}",
        snapshot={},
    )
    db.add(row)
    await db.flush()
    return row


async def test_series_and_number_are_unique_together(db, paid_application, second_paid_application):
    """tz/05 invariant 2. The database is the guarantee, not the service."""
    await _permit(db, paid_application, number=1)
    with pytest.raises(IntegrityError, match="uq_permits_series_number"):
        await _permit(db, second_paid_application, number=1)


async def test_one_permit_per_application(db, paid_application):
    """design/02: application_id is unique — the relation is 1:1."""
    await _permit(db, paid_application, number=1)
    with pytest.raises(IntegrityError, match="application_id"):
        await _permit(db, paid_application, number=2)


async def test_status_history_cannot_be_updated(db, paid_application):
    """tz/05 invariant 6 and the append-only idiom of audit_log, calculations
    and application_status_history."""
    permit = await _permit(db, paid_application)
    row = PermitStatusHistory(
        permit_id=permit.id, from_status=None, to_status="pending_signatures", changed_by=None
    )
    db.add(row)
    await db.flush()
    row.to_status = "active"
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_the_counter_hands_out_each_number_once(db):
    """Ruling 9: UPDATE ... RETURNING under the row lock, not SELECT-then-UPDATE."""
    first = await db.scalar(
        text(
            "UPDATE permit_counters SET last_number = last_number + 1"
            " WHERE series = :s RETURNING last_number"
        ).bindparams(s="А")
    )
    second = await db.scalar(
        text(
            "UPDATE permit_counters SET last_number = last_number + 1"
            " WHERE series = :s RETURNING last_number"
        ).bindparams(s="А")
    )
    assert second == first + 1
