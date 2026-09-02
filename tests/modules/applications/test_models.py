from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.applications.models import Application


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


async def test_applications_permission_seeds(db) -> None:
    """ruling 16: create -> applicant, review -> executor_staff ('hodim' in the
    plan's prose), decide -> leadership, view_any -> prosecutor,
    assign -> sys_admin. Wrong role codes in the migration insert zero rows
    silently (.claude/lessons.md) — this is the guard."""
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
        ("leadership", "applications.decide"),
        ("prosecutor", "applications.view_any"),
        ("sys_admin", "applications.assign"),
    }
