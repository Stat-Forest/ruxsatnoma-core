import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.permits.models import Permit, PermitStatusHistory, PermitTemplate


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


async def _template(db, activity_type_id, *, version: int, status="active") -> PermitTemplate:
    row = PermitTemplate(
        activity_type_id=activity_type_id,
        version=version,
        name={"uz_cyrl": f"Шаблон v{version}", "ru": f"Шаблон v{version}"},
        status=status,
        valid_from=date(2027, 1, 1),
    )
    db.add(row)
    await db.flush()
    return row


async def test_only_one_template_version_is_active_per_activity_type(db, haymaking_activity_id):
    """Review round 1: `uq(activity_type_id, version)` alone lets two ACTIVE rows
    exist, so Task 3's "the active template for this activity" lookup would return
    whichever row the plan order handed back and freeze the wrong `template_id` into
    the permit permanently. Every sibling versioned catalogue (notification_templates
    0009, contour_versions) pins this with the same partial unique index."""
    await _template(db, haymaking_activity_id, version=1)
    with pytest.raises(IntegrityError, match="uq_permit_templates_active"):
        await _template(db, haymaking_activity_id, version=2)


async def test_archiving_the_active_template_frees_the_slot_for_the_next_version(
    db, haymaking_activity_id
):
    """The supersede the docstring promises, in the ONE order that works: archive,
    `flush()`, then insert. Without the flush both statements are still pending when
    the partial index is checked and the insert raises on a conflict the flush would
    have resolved (lesson). Archived rows sit outside the index, so the superseded
    version stays readable — an issued permit's `template_id` still resolves."""
    first = await _template(db, haymaking_activity_id, version=1)

    first.status = "archived"
    await db.flush()
    second = await _template(db, haymaking_activity_id, version=2)

    assert second.status == "active"
    assert first.status == "archived"
