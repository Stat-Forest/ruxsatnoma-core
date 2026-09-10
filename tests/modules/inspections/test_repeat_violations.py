"""Ruling R8 (finding F3, `plans/07.5-audit-findings.md`): tz/04's own line —
"Повторное нарушение → система показывает историю" (a repeat violation -> the
system shows the history) — had no code behind it: no filter to pull up a
violator's earlier cases, and no count on the case card itself. The
"suggests stricter" half is deliberately NOT built (ruling R8): no rule in
the spec says how much stricter, and a suggested penalty the system cannot
justify is worse than the facts alone."""

import uuid

from app.modules.applications.models import Application
from app.modules.inspections import repo, service
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from tests.modules.inspections.conftest import make_application

API = "/api/v1/inspections"


async def _open_case_for(
    db,
    inspector,
    inspector_client,
    application: Application,
    checklist_id: uuid.UUID,
    vt_id: uuid.UUID,
) -> dict:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(checklist_id),
            "answers": {"activity_matches": True, "within_contour": False},
            "result": "violation",
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
    signed = await inspector_client.post(
        f"{API}/acts/{act_id}/sign",
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_id)},
    )
    assert signed.status_code == 200, signed.text
    case = await repo.case_for_act(db, act.id)
    assert case is not None
    return {"case_id": str(case.id), "number": case.number}


async def test_applicant_id_filter_returns_only_that_applicants_cases(
    db,
    executor_head_client,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
    contours_layer,
    leshoz,
    approval_doc,
    grazing_activity_id: uuid.UUID,
) -> None:
    case_1 = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )
    case_2 = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )
    # A SECOND, independent application — a different applicant entirely —
    # must never show up in the first applicant's own filtered list.
    other_application = await make_application(
        db,
        layer=contours_layer,
        org=leshoz,
        approval_doc=approval_doc,
        activity_type_id=grazing_activity_id,
    )
    other_case = await _open_case_for(
        db, inspector, inspector_client, other_application, default_checklist_id, vt_01
    )

    resp = await executor_head_client.get(
        f"{API}/cases", params={"applicant_id": str(application.applicant_id)}
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {case_1["case_id"], case_2["case_id"]}
    assert other_case["case_id"] not in ids


async def test_the_list_puts_the_most_recently_updated_case_first(
    db,
    executor_head_client,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> None:
    """`updated_at DESC`, `id DESC` only as the tie-break — the same rule
    `GET /applications` and `GET /permits` follow: the older case, once the
    head asks the violator for an explanation, climbs above the newer one
    nobody has touched. The `applicant_id` filter makes the list exact."""
    older = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )
    newer = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )

    touched = await executor_head_client.post(f"{API}/cases/{older['case_id']}/request-explanation")
    assert touched.status_code == 200, touched.text

    resp = await executor_head_client.get(
        f"{API}/cases", params={"applicant_id": str(application.applicant_id)}
    )
    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()["items"]] == [older["case_id"], newer["case_id"]]


async def test_the_applicant_id_filter_still_obeys_the_case_scope(
    db,
    executor_head_client,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
    other_leshoz,
) -> None:
    """A head zoned to a DIFFERENT leshoz filtering by the SAME applicant_id
    sees nothing — the filter is intersected with `_case_scope`, never a
    replacement for it."""
    from tests.modules.auth.test_sessions import make_user
    from tests.modules.inspections.conftest import _client_for_user

    await _open_case_for(db, inspector, inspector_client, application, default_checklist_id, vt_01)

    other_head = await make_user(db, role_code="executor_head", organization_id=other_leshoz.id)
    async with _client_for_user(db, other_head) as client:
        resp = await client.get(
            f"{API}/cases", params={"applicant_id": str(application.applicant_id)}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_prior_cases_count_is_decided_and_closed_only_excluding_itself(
    db,
    executor_head_client,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> None:
    decided_case = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )
    decide_resp = await executor_head_client.post(
        f"{API}/cases/{decided_case['case_id']}/decide", json={"decision": "warning"}
    )
    assert decide_resp.status_code == 200, decide_resp.text

    closed_case = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )
    await executor_head_client.post(
        f"{API}/cases/{closed_case['case_id']}/decide", json={"decision": "warning"}
    )
    close_resp = await executor_head_client.post(f"{API}/cases/{closed_case['case_id']}/close")
    assert close_resp.status_code == 200, close_resp.text

    # Still open — must NOT count as a "prior" case.
    still_open_case = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )

    fresh_case = await _open_case_for(
        db, inspector, inspector_client, application, default_checklist_id, vt_01
    )

    card = await executor_head_client.get(f"{API}/cases/{fresh_case['case_id']}")
    assert card.status_code == 200, card.text
    assert card.json()["prior_cases_count"] == 2

    still_open_card = await executor_head_client.get(f"{API}/cases/{still_open_case['case_id']}")
    assert still_open_card.status_code == 200
    # The still-open case has the same two priors (decided_case, closed_case)
    # and does not count ITSELF or `fresh_case` (also still open).
    assert still_open_card.json()["prior_cases_count"] == 2


async def test_a_case_with_no_identified_applicant_has_a_zero_prior_count(
    db, inspector, inspector_client, default_checklist_id: uuid.UUID, vt_01: uuid.UUID
) -> None:
    """An "activity without a permit" act (no application/permit) opens a
    case with `applicant_id IS NULL` — nothing to count history against."""
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "occurred_at": "2027-06-01T10:00:00Z",
            "gps": {"lon": 69.25, "lat": 41.32},
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": False},
            "result": "violation",
        },
    )
    assert created.status_code == 201, created.text
    act_id = uuid.UUID(created.json()["id"])
    act = await repo.get_act(db, act_id)
    assert act is not None
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
    assert case.applicant_id is None

    card = await inspector_client.get(f"{API}/cases/{case.id}")
    assert card.status_code == 200, card.text
    assert card.json()["prior_cases_count"] == 0
