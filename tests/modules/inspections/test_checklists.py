"""`POST /inspections/checklists` — the builder (`CHECKLISTS_MANAGE`),
superseding by archive-then-insert rather than an in-place edit."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inspections.models import Checklist


def _payload(code: str) -> dict:
    return {
        "code": code,
        "name": {"uz_cyrl": "Тест чек-листи", "uz_latn": "Test chek-listi"},
        "items": [
            {
                "code": "q1",
                "question": {"uz_cyrl": "Савол 1?", "uz_latn": "Savol 1?"},
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


async def test_a_legacy_item_code_over_the_current_bound_is_still_listed(
    db: AsyncSession, central_admin_client
) -> None:
    """I5, final review. `ChecklistQuestion.code` (the IN-side item type) is
    `CodeStr`, capped at 64 — but `ChecklistOut.items` used to reuse that
    SAME type to validate what it reads back, so a row stored before the
    stage-17 bound existed (or written directly, as this test does) would
    500 the whole `GET /inspections/checklists` list the moment pydantic
    tried to re-validate its 100-character code against a 64-character cap.
    `ChecklistOut` now uses `ChecklistQuestionOut`, whose `code` is a plain
    unbounded `str`, so a legacy row is still listed rather than breaking
    the page for everyone."""
    code = _unique_code("legacy")
    checklist = Checklist(
        code=code,
        version=1,
        name={"uz_latn": "Legacy"},
        items=[
            {
                "code": "x" * 100,
                "question": {"uz_latn": "Legacy question?"},
                "type": "bool",
                "required": True,
            }
        ],
        status="active",
    )
    db.add(checklist)
    await db.commit()

    listed = await central_admin_client.get("/api/v1/inspections/checklists")
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["code"] == code)
    assert row["items"][0]["code"] == "x" * 100
