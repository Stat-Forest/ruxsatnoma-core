"""`POST /inspections/checklists` — the builder (`CHECKLISTS_MANAGE`),
superseding by archive-then-insert rather than an in-place edit."""

import uuid


def _payload(code: str) -> dict:
    return {
        "code": code,
        "name": {"uz_cyrl": "Тест чек-листи"},
        "items": [
            {
                "code": "q1",
                "question": {"uz_cyrl": "Савол 1?"},
                "type": "bool",
                "required": True,
            }
        ],
    }


def _unique_code(prefix: str) -> str:
    """Never a fixed literal: `checklists` commits through the app's own
    session (`_client_for`-style clients commit for real), so a fixed code
    would accumulate versions across runs on the shared, persistent test DB
    (lesson) and this file's own version assertions would go stale."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def test_central_admin_creates_a_checklist(central_admin_client) -> None:
    r = await central_admin_client.post(
        "/api/v1/inspections/checklists", json=_payload(_unique_code("t1"))
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["status"] == "active"


async def test_a_second_version_supersedes_the_first(central_admin_client) -> None:
    code = _unique_code("t2")
    first = await central_admin_client.post("/api/v1/inspections/checklists", json=_payload(code))
    assert first.status_code == 201

    second = await central_admin_client.post("/api/v1/inspections/checklists", json=_payload(code))
    assert second.status_code == 201
    assert second.json()["version"] == 2

    listed = await central_admin_client.get("/api/v1/inspections/checklists")
    active = [item for item in listed.json() if item["code"] == code]
    assert len(active) == 1
    assert active[0]["version"] == 2


async def test_executor_head_cannot_manage_checklists(executor_head_client) -> None:
    r = await executor_head_client.post(
        "/api/v1/inspections/checklists", json=_payload(_unique_code("t3"))
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"
