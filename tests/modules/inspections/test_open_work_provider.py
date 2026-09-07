"""`inspections.service.open_work_provider` — this module's own answer to
`admin.open_work`'s question (ruling R4/R5, `07.6-handover-and-the-violator.md`;
finding F4, `07.5-audit-findings.md`: an inspection task had NO recovery route
at all before this stage).

Tested directly, in-process, against the SERVICE function itself — not only
through an HTTP scenario — because this is exactly the "public surface task's
own end-to-end test can ship the surface untested" shape a lesson in this
codebase names: a caller in ANOTHER module (`admin.users_service.delete_user`,
via the registry) is what actually calls this, and that caller does not exist
on this branch yet (track A's Task 1). `app/event_subscriptions.py`'s own
registration of this provider is exercised together with the admin-side guard
in the integration pass (plan Task 8)."""

import uuid

from app.modules.admin.open_work import OpenWork
from app.modules.applications.models import Application
from app.modules.inspections import service

API = "/api/v1/inspections"


async def test_a_user_with_no_tasks_reports_no_open_work(db, inspector) -> None:
    assert await service.open_work_provider(db, inspector.id) is None


async def test_an_assigned_task_is_open_work(
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
    task_id = uuid.UUID(created.json()["id"])

    result = await service.open_work_provider(db, inspector.id)

    assert result is not None
    assert isinstance(result, OpenWork)
    assert result.kind == "inspection_tasks"
    assert result.count == 1
    assert result.ids == [task_id]


async def test_an_in_progress_task_still_counts(
    db, executor_head_client, inspector_client, application: Application, inspector
) -> None:
    created = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    task_id = created.json()["id"]
    started = await inspector_client.post(f"{API}/tasks/{task_id}/start")
    assert started.status_code == 200, started.text

    result = await service.open_work_provider(db, inspector.id)

    assert result is not None
    assert result.count == 1


async def test_a_cancelled_task_does_not_count(
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
    task_id = created.json()["id"]
    cancelled = await executor_head_client.post(f"{API}/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    assert await service.open_work_provider(db, inspector.id) is None
