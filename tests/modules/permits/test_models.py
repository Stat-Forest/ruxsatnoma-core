import uuid
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.permits import repo
from app.modules.permits.models import (
    PERMIT_STATUSES,
    Permit,
    PermitStatusHistory,
    PermitTemplate,
)
from app.modules.permits.schemas import PermitStatus


async def _permit(
    db, application, *, series="А", number: int | None = None, status="pending_signatures"
) -> Permit:
    """`number` defaults to the NEXT one the counter hands out, never a literal:
    `test_issue.py` issues real permits whose rows COMMIT, so numbers 1..N are
    permanently taken on this shared, persistent test DB and a hard-coded 1 fails
    on `uq_permits_series_number` (lesson). The allocation itself rolls back with
    the test, so the same number is free again for the next one."""
    if number is None:
        number = await repo.next_number(db, series)
        assert number is not None
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
    taken = await _permit(db, paid_application)
    with pytest.raises(IntegrityError, match="uq_permits_series_number"):
        await _permit(db, second_paid_application, number=taken.number)


async def test_one_permit_per_application(db, paid_application):
    """design/02: application_id is unique — the relation is 1:1."""
    await _permit(db, paid_application)
    with pytest.raises(IntegrityError, match="application_id"):
        await _permit(db, paid_application)


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


async def test_the_schema_literals_match_the_tables_own_check_constraints() -> None:
    """The one guard against `schemas.PermitStatus` drifting from the tuple the
    CHECK is built from (lesson: an enum-ish column has ONE source of truth). The
    members have to be written out — pyright rejects a starred variable inside
    `Literal` — so a status added on one side and forgotten on the other would be
    a 422 that should have been a 201, or an IntegrityError 500 that should have
    been a 422."""
    assert set(get_args(PermitStatus)) == set(PERMIT_STATUSES)
