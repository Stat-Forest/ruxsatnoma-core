from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import uuid7
from app.modules.applications.models import (
    APPLICATION_STATUSES,
    CHECK_TYPES,
    Application,
    ApplicationCheck,
    ApplicationStatusHistory,
)


async def _app(
    db,
    applicant,
    contour_id,
    activity_id,
    *,
    status="SUBMITTED",
    frm=date(2027, 5, 1),
    to=date(2027, 9, 30),
) -> Application:
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        activity_type_id=activity_id,
        contour_id=contour_id,
        period_from=frm,
        period_to=to,
        status=status,
        channel="portal",
    )
    db.add(row)
    await db.flush()
    return row


async def test_two_active_applications_on_an_overlapping_period_are_refused(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """tz/05 invariant 1: one active application per
    (applicant + contour + activity + overlapping period). The database is the
    only detector (ruling 6) — a pre-SELECT would race."""
    await _app(db, applicant, published_contour.id, grazing_activity_id)
    with pytest.raises(IntegrityError, match="ex_applications_no_duplicate"):
        await _app(
            db,
            applicant,
            published_contour.id,
            grazing_activity_id,
            frm=date(2027, 9, 1),
            to=date(2027, 11, 30),
        )


async def test_two_drafts_on_the_same_contour_are_allowed(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """DRAFT is outside the constraint's WHERE clause (design/02): a duplicate
    is caught at submission, not while the applicant is still typing."""
    await _app(db, applicant, published_contour.id, grazing_activity_id, status="DRAFT")
    await _app(db, applicant, published_contour.id, grazing_activity_id, status="DRAFT")


async def test_status_check_rejects_a_bogus_value(db, applicant) -> None:
    """I2: `ck_applications_status_valid` is the constraint ruling 2 built a whole
    paragraph around, because the plan's own earlier draft enumerated thirteen
    values after promising fourteen. Contour/activity/period left null (ruling 7)
    isolates the status CHECK from the EXCLUDE constraint entirely."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="BOGUS",
    )
    db.add(row)
    with pytest.raises(IntegrityError, match="ck_applications_status_valid"):
        await db.flush()


async def test_status_check_accepts_every_status(db, applicant) -> None:
    """The positive half of I2: every OTHER test in this file only ever writes
    SUBMITTED or DRAFT, so a CHECK one status short of ruling 2's fourteen would
    still pass all of them. Driven off APPLICATION_STATUSES itself, never a
    retyped list — the two cannot drift apart by construction."""
    for status in APPLICATION_STATUSES:
        row = Application(
            applicant_id=applicant.id,
            submitted_by_user_id=applicant.owner_user_id,
            on_behalf="self",
            channel="portal",
            status=status,
        )
        db.add(row)
        await db.flush()


async def test_check_type_check_rejects_a_bogus_value(db, applicant) -> None:
    """I2: `application_checks` had no test at all before this. `gis_restrictions`
    is design/02's own value, dropped by ruling 21 in favour of `norm_restrictions`
    — using it as the negative case doubles as a regression guard against it
    quietly coming back."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    check = ApplicationCheck(
        application_id=app_row.id, check_type="gis_restrictions", result="pass", details={}
    )
    db.add(check)
    with pytest.raises(IntegrityError, match="ck_application_checks_check_type_valid"):
        await db.flush()


async def test_check_type_check_accepts_every_check_type(db, applicant) -> None:
    """The positive half, ruling 21's eleven values, tuple-driven — the same
    one-short failure mode as the status CHECK, on a table nothing else in this
    file constructs at all."""
    app_row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
    )
    db.add(app_row)
    await db.flush()
    for check_type in CHECK_TYPES:
        check = ApplicationCheck(
            application_id=app_row.id, check_type=check_type, result="pass", details={}
        )
        db.add(check)
        await db.flush()


async def test_status_history_cannot_be_updated(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """tz/05 invariant 6 + the append-only idiom of audit_log and calculations.

    `DBAPIError`, not `IntegrityError`: the trigger's plain `RAISE EXCEPTION` has
    no SQLSTATE in the integrity-constraint-violation class, so it surfaces as the
    broader `DBAPIError` — the same as `tests/modules/audit/test_audit_log.py` and
    `tests/modules/norms/test_models.py` assert for the identical trigger idiom."""
    from app.modules.applications.models import ApplicationStatusHistory

    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    row = ApplicationStatusHistory(
        application_id=app_row.id, from_status=None, to_status="DRAFT", changed_by=None
    )
    db.add(row)
    await db.flush()
    row.to_status = "APPROVED"
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_status_history_cannot_be_deleted(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """M2: UPDATE and DELETE share one `CREATE TRIGGER ... FOR EACH ROW` statement
    so they stand or fall together in principle, but only UPDATE had a test."""
    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    row = ApplicationStatusHistory(
        application_id=app_row.id, from_status=None, to_status="DRAFT", changed_by=None
    )
    db.add(row)
    await db.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("DELETE FROM application_status_history WHERE id = :id"), {"id": row.id}
        )


async def test_status_history_cannot_be_truncated(db) -> None:
    """M2: `application_status_history_no_truncate` is a SEPARATE `CREATE TRIGGER`
    statement from the UPDATE/DELETE one (`FOR EACH STATEMENT`, not `FOR EACH
    ROW`) — a typo in its clause or its table name would ship unnoticed."""
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("TRUNCATE application_status_history"))


async def test_status_history_id_can_be_supplied_explicitly(
    db, applicant, published_contour, grazing_activity_id
) -> None:
    """M3 / ruling 25: `submit` supplies `application_status_history.id` explicitly
    — it is the submission id the signature is bound to. Asserted nowhere until
    now; `id` keeps its `uuid7` default (models.py), which a caller-supplied value
    simply overrides, the same as any other Python-side `mapped_column(default=)`."""
    app_row = await _app(db, applicant, published_contour.id, grazing_activity_id)
    submission_id = uuid7()
    row = ApplicationStatusHistory(
        id=submission_id,
        application_id=app_row.id,
        from_status=None,
        to_status="SUBMITTED",
        changed_by=None,
    )
    db.add(row)
    await db.flush()
    assert row.id == submission_id


async def test_applications_permission_seeds(db) -> None:
    """ruling 16: create -> applicant, review -> executor_staff ('hodim' in the
    plan's prose), decide -> executor_head AND leadership (review round 1 finding
    I3 — tz/03's matrix gives approve/sign on an application to executor_head, not
    leadership alone), view_any -> prosecutor, assign -> sys_admin. Wrong role codes
    in the migration insert zero rows silently (.claude/lessons.md) — this is the
    guard."""
    rows = await db.execute(
        text(
            "SELECT r.code, rp.permission_code FROM role_permissions rp"
            " JOIN roles r ON r.id = rp.role_id"
            " WHERE rp.permission_code LIKE 'applications.%'"
        )
    )
    assert {(row[0], row[1]) for row in rows} == {
        ("applicant", "applications.create"),
        ("executor_staff", "applications.review"),
        ("executor_head", "applications.decide"),
        ("leadership", "applications.decide"),
        ("prosecutor", "applications.view_any"),
        ("sys_admin", "applications.assign"),
    }
