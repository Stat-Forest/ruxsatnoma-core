"""`POST /inspections/tasks` and its lifecycle (tz/04 С6/С15) — assignment,
ownership on `start`, `TASKS_MANAGE` on `cancel`, and zone scoping."""

from app.modules.applications.models import Application

API = "/api/v1/inspections"


async def test_executor_head_creates_a_task(
    executor_head_client, application: Application, inspector
) -> None:
    r = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "permit_inspection"
    assert body["status"] == "assigned"
    assert body["assigned_to"] == str(inspector.id)
    # Default due date: 2 working days out (tz/04 С6).
    assert body["due_at"] is not None


async def test_executor_staff_also_holds_tasks_manage(
    executor_staff_client, application: Application, inspector
) -> None:
    """Migration `0026`'s own grant — the ходим who raises a C6 site visit."""
    r = await executor_staff_client.post(
        f"{API}/tasks",
        json={
            "kind": "pre_approval_visit",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert r.status_code == 201, r.text


async def test_inspector_cannot_create_a_task(
    inspector_client, application: Application, inspector
) -> None:
    r = await inspector_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_task_needs_at_least_one_subject(executor_head_client, inspector) -> None:
    r = await executor_head_client.post(
        f"{API}/tasks", json={"kind": "permit_inspection", "assigned_to": str(inspector.id)}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_cross_zone_task_creation_is_refused(
    db, other_leshoz, application: Application, inspector
) -> None:
    """`application`'s org is `leshoz`; an executor_head zoned to a DIFFERENT
    leshoz may not raise a task against it (`_assert_organization_in_zone`)."""
    from tests.modules.auth.test_sessions import make_user
    from tests.modules.inspections.conftest import _client_for_user

    other_head = await make_user(db, role_code="executor_head", organization_id=other_leshoz.id)
    async with _client_for_user(db, other_head) as client:
        r = await client.post(
            f"{API}/tasks",
            json={
                "kind": "permit_inspection",
                "application_id": str(application.id),
                "assigned_to": str(inspector.id),
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-002"


async def test_started_task_moves_to_in_progress(
    inspector_client, executor_head_client, application: Application, inspector
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

    r = await inspector_client.post(f"{API}/tasks/{task_id}/start")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_progress"


async def test_only_the_assignee_may_start_their_task(
    executor_head_client, other_inspector_client, application: Application, inspector
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

    r = await other_inspector_client.post(f"{API}/tasks/{task_id}/start")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_executor_head_cancels_a_task(
    executor_head_client, application: Application, inspector
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

    r = await executor_head_client.post(f"{API}/tasks/{task_id}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # A cancelled task cannot be started.
    again = await executor_head_client.post(f"{API}/tasks/{task_id}/cancel")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-INSP-001"


async def test_inspector_lists_only_their_own_tasks(
    executor_head_client,
    inspector_client,
    other_inspector_client,
    application: Application,
    inspector,
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

    mine = await inspector_client.get(f"{API}/tasks")
    assert mine.status_code == 200
    assert any(item["id"] == task_id for item in mine.json()["items"])

    theirs = await other_inspector_client.get(f"{API}/tasks")
    assert theirs.status_code == 200
    assert not any(item["id"] == task_id for item in theirs.json()["items"])


async def test_executor_head_sees_every_task_in_zone(
    executor_head_client, application: Application, inspector
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

    r = await executor_head_client.get(f"{API}/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["id"] == task_id
