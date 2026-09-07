"""`POST /inspections/tasks/{id}/reassign` (ruling R6, plan
`07.6-handover-and-the-violator.md`) — the handover tz/04 С23 assumes exists.
Until this stage the only way off a task was `cancel`, which discards it: a
departing inspector's unfinished field task lost its due date and its
history (finding F4, second shape, `plans/07.5-audit-findings.md`).

The other half of F4 — that a delete IS unblocked once the handover happens
— needs `admin.users_service.delete_user` to consult the open-work registry
(track A's Task 1), which does not exist on this branch. That combined
end-to-end path is verified in the integration pass (plan Task 8), not
here."""

import uuid

import pytest
from sqlalchemy import select

from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.inspections import repo, service
from app.modules.inspections.models import InspectionTask
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from tests.modules.inspections.conftest import unique_pinfl

API = "/api/v1/inspections"


@pytest.fixture
async def second_inspector(db, leshoz: Organization) -> User:
    """A SECOND inspector in the SAME leshoz as `assigned_task` — the
    ordinary handover target. `other_inspector` (a DIFFERENT leshoz) stays
    reserved for the cross-zone REFUSAL test."""
    from tests.modules.auth.test_sessions import make_user

    return await make_user(
        db, role_code="inspector", organization_id=leshoz.id, pinfl=unique_pinfl()
    )


@pytest.fixture
async def assigned_task(
    db, executor_head_client, application: Application, inspector
) -> InspectionTask:
    created = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert created.status_code == 201, created.text
    task = await repo.get_task(db, uuid.UUID(created.json()["id"]))
    assert task is not None
    return task


@pytest.fixture
async def in_progress_task(db, inspector_client, assigned_task: InspectionTask) -> InspectionTask:
    r = await inspector_client.post(f"{API}/tasks/{assigned_task.id}/start")
    assert r.status_code == 200, r.text
    await db.refresh(assigned_task)
    return assigned_task


@pytest.fixture
async def done_task(
    db,
    inspector,
    inspector_client,
    assigned_task: InspectionTask,
    default_checklist_id: uuid.UUID,
) -> InspectionTask:
    """A FINISHED task, reached through the real completion path (signing an
    act tied to it) — never by hand-setting `status` (lesson: "Build a
    fixture's precondition through the real transition")."""
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "task_id": str(assigned_task.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": True},
            "result": "compliant",
        },
    )
    assert created.status_code == 201, created.text
    act_id = created.json()["id"]
    act = await repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    pkcs7 = encode_mock_signature(
        document=service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(f"{API}/acts/{act_id}/sign", json={"pkcs7": pkcs7})
    assert signed.status_code == 200, signed.text
    await db.refresh(assigned_task)
    assert assigned_task.status == "done"
    return assigned_task


async def test_a_task_moves_to_another_inspector_and_keeps_its_identity(
    db, executor_head_client, assigned_task: InspectionTask, second_inspector: User
) -> None:
    resp = await executor_head_client.post(
        f"{API}/tasks/{assigned_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_to"] == str(second_inspector.id)
    assert body["id"] == str(assigned_task.id), "the same task, not a new one"
    assert body["due_at"] == assigned_task.due_at.isoformat(), "the clock does not restart"
    assert body["status"] == "assigned"


async def test_a_task_in_progress_can_also_be_handed_over(
    db, executor_head_client, in_progress_task: InspectionTask, second_inspector: User
) -> None:
    resp = await executor_head_client.post(
        f"{API}/tasks/{in_progress_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["assigned_to"] == str(second_inspector.id)
    assert resp.json()["status"] == "in_progress", "the handover does not reset progress"


async def test_a_finished_task_is_not_reassignable(
    db, executor_head_client, done_task: InspectionTask, second_inspector: User
) -> None:
    resp = await executor_head_client.post(
        f"{API}/tasks/{done_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ERR-INSP-001"


async def test_the_new_assignee_must_be_inside_the_tasks_own_zone(
    db, executor_head_client, assigned_task: InspectionTask, other_inspector
) -> None:
    """`other_inspector` is zoned to a DIFFERENT leshoz than `assigned_task`'s
    own organization — the candidate's own zone is what is checked, not the
    head's."""
    resp = await executor_head_client.post(
        f"{API}/tasks/{assigned_task.id}/reassign",
        json={"new_assignee_id": str(other_inspector.id)},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-002"


async def test_the_handover_is_audited_with_both_sides(
    db, executor_head_client, assigned_task: InspectionTask, second_inspector: User, inspector
) -> None:
    resp = await executor_head_client.post(
        f"{API}/tasks/{assigned_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == service.TASK_REASSIGN,
                AuditLog.object_id == assigned_task.id,
            )
        )
    ).scalar_one()
    assert row.old_value == {"assigned_to": str(inspector.id)}
    assert row.new_value == {"assigned_to": str(second_inspector.id)}


async def test_only_tasks_manage_may_reassign(
    db, inspector_client, assigned_task: InspectionTask, second_inspector: User
) -> None:
    resp = await inspector_client.post(
        f"{API}/tasks/{assigned_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ERR-ACL-001"


# --- The integration pass's own test (plan 07.6 task 8) ---------------------
#
# Neither track could write this one: the refusal lives in `admin`
# (`delete_user`'s open-work guard, track A) and the handover lives here
# (track B), and each branch had only its own half. Four of stage 7.4's ten
# findings were visible ONLY once the tracks were on one branch, which is why
# the integration pass is not optional — and why this test lives on the
# integration branch rather than in either track's file.


async def test_the_handover_is_what_unblocks_the_delete(
    db, executor_head_client, assigned_task: InspectionTask, inspector: User, second_inspector: User
):
    """F4's two halves meeting, walked as ONE path rather than as two.

    `tz/04` С23 asks for exactly this sentence — "удаление — только после
    передачи незавершённых дел другому исполнителю" — and until stage 7.6 the
    system had neither half: the delete never refused, and there was no way to
    hand a field task over even if it had.

    Asserting the WHOLE path matters more than either half. A guard that
    refuses forever is as broken as one that never refuses, and a test that
    only proved the refusal would pass just as happily on that.
    """
    from app.core.errors import DomainError
    from app.modules.admin import users_service
    from tests.modules.admin.conftest import unique_pinfl as admin_unique_pinfl
    from tests.modules.auth.test_sessions import make_user

    sys_admin = await make_user(db, role_code="sys_admin", pinfl=admin_unique_pinfl())

    with pytest.raises(DomainError) as refused:
        await users_service.delete_user(db, user_id=inspector.id, actor=sys_admin)
    assert refused.value.code == "ERR-VAL-001"
    assert refused.value.details is not None
    held = {item["kind"]: item for item in refused.value.details["open_work"]}
    assert str(assigned_task.id) in held["inspection_tasks"]["ids"], (
        "the refusal must name the task standing in the way — an admin told only "
        "'no' cannot act on it"
    )

    handed_over = await executor_head_client.post(
        f"{API}/tasks/{assigned_task.id}/reassign",
        json={"new_assignee_id": str(second_inspector.id)},
    )
    assert handed_over.status_code == 200, handed_over.text

    deleted = await users_service.delete_user(db, user_id=inspector.id, actor=sys_admin)
    assert deleted.status == "deleted"

    await db.refresh(assigned_task)
    assert assigned_task.assigned_to == second_inspector.id, (
        "and the work itself survived the departure, on its original row"
    )
