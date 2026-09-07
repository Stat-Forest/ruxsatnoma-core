"""tz/04 С23's "удаление — только после передачи незавершённых дел другому
исполнителю" (user deletion only after handing unfinished cases to another
executor) applied to `inspections`' own open work: an assigned field task.

`tests/modules/admin/test_deletion_with_open_cases.py` shows `admin.
users_service.delete_user` has no open-work check at all, using an
`applications` assignment. This file shows the SAME gap reaches
`inspection_tasks`, and that the consequence here is worse than on
`applications`: `applications` at least has a manual escape hatch
(`POST /applications/{id}/assign`, sys_admin-only, 3.9b) a caller could use to
recover after the fact. `inspections` has no reassignment route at all
(`grep -n "reassign" app/modules/inspections/*.py` finds nothing) — the only
way off a task whose inspector was deleted is `TASKS_MANAGE`'s `cancel`
(losing the task entirely) followed by creating a brand new one."""

import uuid

from app.modules.admin import users_service
from app.modules.applications.models import Application
from tests.modules.auth.test_sessions import make_user
from tests.modules.inspections.conftest import _client_for_user

API = "/api/v1/inspections"


async def test_deleting_an_inspector_orphans_their_assigned_task(
    db, executor_head_client, application: Application, inspector
) -> None:
    created = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    admin = await make_user(db, role_code="sys_admin", pinfl=f"1{uuid.uuid4().int % 10**13:013d}")
    await db.flush()
    await db.commit()

    async with _client_for_user(db, admin) as admin_client:
        deleted = await admin_client.post(f"/api/v1/admin/users/{inspector.id}/delete")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"

    # The task is untouched: still "assigned", still pointing at an account
    # that can no longer authenticate (sessions were revoked by the delete)
    # and that `inspections.service._readable_task`'s own-assignee branch can
    # never again satisfy through a live session.
    await db.refresh(inspector)
    assert inspector.status == "deleted"
    card = await executor_head_client.get(f"{API}/tasks/{task_id}")
    assert card.status_code == 200
    assert card.json()["status"] == "assigned"
    assert card.json()["assigned_to"] == str(inspector.id)


async def test_delete_user_service_itself_performs_no_open_work_check(db, inspector) -> None:
    """Same fact, one level down: calling `users_service.delete_user` directly
    (bypassing the route/permission layer entirely) still succeeds — the gap
    is in the SERVICE, not merely in a route that forgot a guard."""
    admin = await make_user(db, role_code="sys_admin", pinfl=f"1{uuid.uuid4().int % 10**13:013d}")
    await db.flush()

    result = await users_service.delete_user(db, user_id=inspector.id, actor=admin)
    assert result.status == "deleted"
