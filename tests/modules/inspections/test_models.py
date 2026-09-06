"""DB-level invariants: the append-only trigger on `violation_case_history`
and migration `0026`'s own reference seeds (classifier items, the default
checklist, role grants)."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.inspections.models import ViolationCase, ViolationCaseHistory


async def _open_case(
    db: AsyncSession, application: Application, *, vt_01: uuid.UUID
) -> ViolationCase:
    from app.modules.inspections.models import InspectionAct, InspectionTask

    task = InspectionTask(
        kind="permit_inspection",
        application_id=application.id,
        assigned_to=application.submitted_by_user_id,
        due_at=application.period_from,
    )
    db.add(task)
    await db.flush()
    checklist_id = (
        await db.execute(text("SELECT id FROM checklists WHERE code = 'field_inspection_default'"))
    ).scalar_one()
    act = InspectionAct(
        task_id=task.id,
        application_id=application.id,
        inspector_id=application.submitted_by_user_id,
        occurred_at=application.period_from,
        checklist_id=checklist_id,
        answers={},
        facts={},
        result="violation",
        status="signed",
    )
    db.add(act)
    await db.flush()
    case = ViolationCase(
        number=f"VC-TEST-{uuid.uuid4().hex[:8]}",
        act_id=act.id,
        violation_type_item_id=vt_01,
        status="opened",
    )
    db.add(case)
    await db.flush()
    return case


async def test_violation_case_history_cannot_be_updated(
    db: AsyncSession, application: Application, vt_01: uuid.UUID
) -> None:
    """tz/05's own append-only idiom, extended to this module (plan `04.1-
    inspections`, mirroring `audit_log`/`calculations`/`application_status_
    history`/`permit_status_history`)."""
    case = await _open_case(db, application, vt_01=vt_01)
    row = ViolationCaseHistory(case_id=case.id, from_status=None, to_status="opened")
    db.add(row)
    await db.flush()
    row.to_status = "closed"
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_violation_case_history_cannot_be_deleted(
    db: AsyncSession, application: Application, vt_01: uuid.UUID
) -> None:
    case = await _open_case(db, application, vt_01=vt_01)
    row = ViolationCaseHistory(case_id=case.id, from_status=None, to_status="opened")
    db.add(row)
    await db.flush()
    await db.delete(row)
    with pytest.raises(DBAPIError, match="append-only"):
        await db.flush()


async def test_violation_types_classifier_has_six_seeded_items(db: AsyncSession) -> None:
    """Migration `0026`: VT-01…06 on the classifier `0005_admin_seeds.py`
    already created empty."""
    codes = (
        (
            await db.execute(
                text(
                    "SELECT ci.code FROM classifier_items ci "
                    "JOIN classifiers c ON c.id = ci.classifier_id "
                    "WHERE c.code = 'violation_types' AND ci.status = 'active' "
                    "ORDER BY ci.code"
                )
            )
        )
        .scalars()
        .all()
    )
    assert codes == ["VT-01", "VT-02", "VT-03", "VT-04", "VT-05", "VT-06"]


async def test_default_checklist_is_seeded_and_active(db: AsyncSession) -> None:
    row = (
        await db.execute(
            text(
                "SELECT status, jsonb_array_length(items) FROM checklists "
                "WHERE code = 'field_inspection_default'"
            )
        )
    ).one()
    assert row[0] == "active"
    assert row[1] >= 2


async def test_role_grants_from_migration_0026(db: AsyncSession) -> None:
    """The five `inspections.*` codes plus the `inspector` -> `permits.view_any`
    fix (plan ruling 3) — a direct check on `role_permissions`, independent of
    any HTTP round-trip."""
    rows = set(
        (
            await db.execute(
                text(
                    "SELECT r.code, rp.permission_code FROM role_permissions rp "
                    "JOIN roles r ON r.id = rp.role_id "
                    "WHERE rp.permission_code LIKE 'inspections.%' "
                    "OR (r.code = 'inspector' AND rp.permission_code = 'permits.view_any')"
                )
            )
        ).all()
    )
    assert ("executor_staff", "inspections.tasks.manage") in rows
    assert ("executor_head", "inspections.tasks.manage") in rows
    assert ("inspector", "inspections.acts.write") in rows
    assert ("executor_head", "inspections.view_any") in rows
    assert ("central_admin", "inspections.view_any") in rows
    assert ("leadership", "inspections.view_any") in rows
    assert ("prosecutor", "inspections.view_any") in rows
    assert ("executor_head", "inspections.cases.manage") in rows
    assert ("central_admin", "inspections.checklists.manage") in rows
    assert ("inspector", "permits.view_any") in rows
