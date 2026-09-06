"""tz/04 С16 — a violation case, auto-opened by a signed act's
`result="violation"`, through explanation, decision, appeal and close."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.inspections import repo, service
from app.modules.integrations.adapters.eimzo import encode_mock_signature

API = "/api/v1/inspections"


@pytest.fixture
async def opened_case(
    db: AsyncSession,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> dict:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": False},
            "result": "violation",
        },
    )
    act_id = created.json()["id"]
    act = await repo.get_act(db, uuid.UUID(act_id))
    assert act is not None
    # Serial derived from the pinfl (`tests/modules/permits/conftest.py::
    # Signer`'s own idiom) — never a fixed literal: `certificates` binds
    # `(serial_number, issuer)` permanently on the shared, persistent test DB
    # (the app's own session commits for real), and a fixed pair would
    # collide with a DIFFERENT inspector's certificate the next time this
    # fixture runs.
    pkcs7 = encode_mock_signature(
        document=service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(
        f"{API}/acts/{act_id}/sign",
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
    )
    assert signed.status_code == 200, signed.text

    case = await repo.case_for_act(db, act.id)
    assert case is not None
    return {"case_id": str(case.id), "act_id": act_id, "number": case.number}


async def test_signing_a_violation_act_opens_a_case(
    opened_case: dict, executor_head_client
) -> None:
    r = await executor_head_client.get(f"{API}/cases/{opened_case['case_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "opened"
    assert body["act_id"] == opened_case["act_id"]
    assert body["number"].startswith("VC-")
    assert body["history"][0]["to_status"] == "opened"


async def test_inspector_sees_the_case_they_raised(opened_case: dict, inspector_client) -> None:
    r = await inspector_client.get(f"{API}/cases/{opened_case['case_id']}")
    assert r.status_code == 200


async def test_a_stranger_inspector_cannot_read_the_case(
    opened_case: dict, other_inspector_client
) -> None:
    r = await other_inspector_client.get(f"{API}/cases/{opened_case['case_id']}")
    assert r.status_code == 403


async def test_the_violator_sees_their_own_case(opened_case: dict, applicant_client) -> None:
    r = await applicant_client.get(f"{API}/cases/{opened_case['case_id']}")
    assert r.status_code == 200


async def test_full_decision_flow(
    opened_case: dict, executor_head_client, applicant_client
) -> None:
    case_id = opened_case["case_id"]

    requested = await executor_head_client.post(f"{API}/cases/{case_id}/request-explanation")
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "explanation_requested"
    assert requested.json()["explanation_due_at"] is not None

    submitted = await applicant_client.post(
        f"{API}/cases/{case_id}/explanation", json={"text": "Скот был на выпасе временно."}
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "explained"
    assert submitted.json()["explanation_text"]

    decided = await executor_head_client.post(
        f"{API}/cases/{case_id}/decide",
        json={"decision": "warning", "note": "Первое нарушение"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "decided"
    assert decided.json()["decision"] == "warning"
    assert decided.json()["decided_by"] is not None

    appealed = await applicant_client.post(
        f"{API}/cases/{case_id}/appeal", json={"text": "Не согласен с решением"}
    )
    assert appealed.status_code == 201, appealed.text

    case_after_appeal = await executor_head_client.get(f"{API}/cases/{case_id}")
    assert case_after_appeal.json()["status"] == "appealed"
    assert len(case_after_appeal.json()["appeals"]) == 1

    # `decide_case` refuses an APPEALED case even though `decided` is a
    # structurally valid target for it — only `resolve_appeal` may drive that
    # edge, or the appeal's own result/resolved_by/resolved_at would be
    # silently orphaned.
    redecide = await executor_head_client.post(
        f"{API}/cases/{case_id}/decide", json={"decision": "revoke"}
    )
    assert redecide.status_code == 409
    assert redecide.json()["error"]["code"] == "ERR-INSP-001"

    resolved = await executor_head_client.post(
        f"{API}/cases/{case_id}/appeal/resolve", json={"result": "upheld"}
    )
    assert resolved.status_code == 200, resolved.text

    closed = await executor_head_client.post(f"{API}/cases/{case_id}/close")
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"

    # A closed case cannot be decided again.
    again = await executor_head_client.post(
        f"{API}/cases/{case_id}/decide", json={"decision": "warning"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "ERR-INSP-001"


async def test_only_cases_manage_may_decide(opened_case: dict, inspector_client) -> None:
    r = await inspector_client.post(
        f"{API}/cases/{opened_case['case_id']}/decide", json={"decision": "warning"}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_stranger_applicant_cannot_appeal_someone_else_s_case(
    db, opened_case: dict
) -> None:
    from tests.modules.auth.test_sessions import make_user
    from tests.modules.inspections.conftest import _client_for_user, unique_pinfl

    stranger = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    async with _client_for_user(db, stranger) as client:
        r = await client.post(
            f"{API}/cases/{opened_case['case_id']}/appeal", json={"text": "Это не моё дело"}
        )
    assert r.status_code == 403
