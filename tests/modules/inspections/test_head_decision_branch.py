"""tz/04 С16 — the raxbar's decision on a violation case: "предупреждение /
приостановка / отзыв (С13) / передача в органы" (warning / suspension /
revocation, which routes into С13's own permit-termination scenario /
referral to another authority).

`docs/plans/07.3-findings.md` walked only the `warning` branch
(`test_full_decision_flow` in `test_violation_cases.py`); `suspend`, `revoke`
and `transfer` were never exercised (07.3's own verdict table: "The head's
decision branch ... was not walked"). This file walks them (07.5 audit,
track B) and checks the six questions the audit brief poses for each: does
the transition exist, does it reach the permit, does the applicant see the
consequence, is the deadline computed, is a notification sent, is the audit
row written.

**Headline finding**: `inspections.service` never imports or calls
`notifications.service.notify` — a `grep -rn "notify" app/modules/
inspections/` finds nothing — so NONE of a case's transitions (open,
request-explanation, decide, appeal, resolve, close) ever reaches the
violator through the one channel `backend/CLAUDE.md` names for "how any
module talks to a user". The violator can still SEE the consequence by
opening `GET /inspections/cases/{id}` themselves (`_case_scope` already
admits the case's own applicant) — nothing is refused — but nothing tells
them to look."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.inspections import repo, service
from app.modules.inspections.models import ViolationCase
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.notifications.models import Notification
from app.modules.permits.models import Permit

API = "/api/v1/inspections"


@pytest.fixture
async def permit_linked_case(
    db: AsyncSession,
    inspector,
    inspector_client,
    permit: Permit,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> dict:
    """Same shape as `test_violation_cases.py::opened_case`, but the act names
    a real PERMIT rather than an application — so the auto-opened case carries
    `permit_id`, which is what makes `suspend`/`revoke` meaningful branches to
    walk (`decide_case`'s own docstring: the permit consequence, if any, is a
    SEPARATE act on `permits`' own suspend/revoke routes citing PS-01 —
    ruling 2 of `docs/plans/04.1-inspections.md`)."""
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "permit_id": str(permit.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
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
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
    )
    assert signed.status_code == 200, signed.text

    case = await repo.case_for_act(db, act.id)
    assert case is not None
    assert case.permit_id == permit.id
    return {"case_id": str(case.id), "act_id": act_id, "permit_id": str(permit.id)}


@pytest.mark.parametrize("decision", ["suspend", "revoke"])
async def test_a_permit_consequence_decision_does_not_touch_the_permit(
    decision: str,
    permit_linked_case: dict,
    executor_head_client,
    db: AsyncSession,
    permit: Permit,
) -> None:
    """The transition EXISTS (the case reaches `decided` and records the
    decision) but does NOT "reach the permit" — by design (ruling 2 of the
    plan: "б) Recorded only"), confirmed here as ACTUAL behaviour rather than
    only a docstring's claim. Executing it against the permit is left as a
    fully separate, fully manual second action on `permits`' own
    suspend/revoke endpoints (tested elsewhere, `tests/modules/permits/
    test_signatures.py` and friends) — nothing here drives that second action
    automatically, and nothing tells the caller they still need to."""
    decided = await executor_head_client.post(
        f"{API}/cases/{permit_linked_case['case_id']}/decide",
        json={"decision": decision, "damage_amount": "150000.00", "note": "Далага кирган"},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "decided"
    assert body["decision"] == decision
    assert body["decided_by"] is not None
    assert Decimal(body["damage_amount"]) == Decimal("150000.00")

    await db.refresh(permit)
    assert permit.status == "active"  # untouched by the case decision alone


async def test_transfer_decision_needs_no_permit_and_no_applicant(
    db: AsyncSession,
    inspector,
    inspector_client,
    executor_head_client,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> None:
    """ "Передача в органы" (referral to another authority) is the fourth
    decision value and the one most obviously outside this module's own
    write surface — an "activity without a permit" act (no task/permit/
    application, a bare GPS fix) auto-opens a case with `permit_id=None` AND
    `applicant_id=None` (no identified violator at all), and `decide_case`
    still accepts `transfer` on it. There is no code anywhere in `app/` that
    integrates with an external authority (prosecutor's office, etc.) — the
    decision is a status value and nothing else, same as `warning`."""
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "occurred_at": "2027-06-01T10:00:00Z",
            "gps": {"lon": 69.2, "lat": 41.3},
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": False, "within_contour": False},
            "result": "violation",
        },
    )
    assert created.status_code == 201, created.text
    act = await repo.get_act(db, uuid.UUID(created.json()["id"]))
    assert act is not None
    pkcs7 = encode_mock_signature(
        document=service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(
        f"{API}/acts/{act.id}/sign",
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
    )
    assert signed.status_code == 200, signed.text
    case = await repo.case_for_act(db, act.id)
    assert case is not None
    assert case.permit_id is None
    assert case.applicant_id is None

    decided = await executor_head_client.post(
        f"{API}/cases/{case.id}/decide", json={"decision": "transfer"}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "transfer"


async def test_case_lifecycle_never_notifies_the_violator(
    permit_linked_case: dict,
    executor_head_client,
    applicant_client,
    applicant_user,
    db: AsyncSession,
) -> None:
    """Walks the WHOLE С16 lifecycle — open (already happened in the fixture),
    request-explanation, submit-explanation, decide, appeal, resolve-appeal,
    close — and asserts, at each step, that `notifications` gains no row for
    the violator. Contrast with `permits.decisions.decide` (the SEPARATE
    suspend/revoke act this case's own decision would still need a second
    click for): that path's step 9 is `notifications.notify(...,
    recipient_user_id=service._holder_recipient(...))` — inspections has no
    equivalent anywhere on this path, for ANY of the four decision values,
    not only the two with a permit consequence."""
    case_id = permit_linked_case["case_id"]

    async def notifications_for_applicant() -> int:
        rows = await db.execute(
            select(Notification).where(Notification.recipient_user_id == applicant_user.id)
        )
        return len(rows.scalars().all())

    assert await notifications_for_applicant() == 0  # after case-open

    requested = await executor_head_client.post(f"{API}/cases/{case_id}/request-explanation")
    assert requested.status_code == 200, requested.text
    assert await notifications_for_applicant() == 0

    submitted = await applicant_client.post(
        f"{API}/cases/{case_id}/explanation", json={"text": "Тасодифан юз берди."}
    )
    assert submitted.status_code == 200, submitted.text
    assert await notifications_for_applicant() == 0

    decided = await executor_head_client.post(
        f"{API}/cases/{case_id}/decide", json={"decision": "warning", "note": "Огоҳлантириш"}
    )
    assert decided.status_code == 200, decided.text
    assert await notifications_for_applicant() == 0  # the decision itself is silent

    appealed = await applicant_client.post(
        f"{API}/cases/{case_id}/appeal", json={"text": "Розимасман"}
    )
    assert appealed.status_code == 201, appealed.text
    assert await notifications_for_applicant() == 0

    resolved = await executor_head_client.post(
        f"{API}/cases/{case_id}/appeal/resolve", json={"result": "upheld"}
    )
    assert resolved.status_code == 200, resolved.text
    assert await notifications_for_applicant() == 0

    closed = await executor_head_client.post(f"{API}/cases/{case_id}/close")
    assert closed.status_code == 200, closed.text
    assert await notifications_for_applicant() == 0

    # The violator CAN still find all of this by opening the case themselves —
    # nothing is refused, it is simply never pushed to them.
    card = await applicant_client.get(f"{API}/cases/{case_id}")
    assert card.status_code == 200
    assert card.json()["status"] == "closed"


async def test_no_way_to_find_a_repeat_violators_earlier_cases(
    db: AsyncSession,
    inspector,
    inspector_client,
    executor_head_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> None:
    """tz/04 С16: "Повторное нарушение → система показывает историю,
    предлагает строже" (a repeat violation → the system shows the history,
    suggests a stricter decision). Nothing in `app/modules/inspections/`
    implements this: `repo.py` has no query joining a violator's OTHER cases,
    `case_card` returns only the ONE case's own history/appeals
    (`service.case_card`), and `GET /inspections/cases` accepts only a
    `status` filter (`router.py::list_cases`) — no `applicant_id`/`permit_id`
    to pull up "every case against this violator". This test opens TWO cases
    against the SAME applicant and shows the second case's card carries no
    trace of the first, and that the unsupported filter is silently ignored
    rather than erroring (the same failure shape the lessons file already
    names for `norms`'s paging: "FastAPI ignores an unknown query parameter")."""

    async def open_one_case() -> ViolationCase:
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
        assert created.status_code == 201, created.text
        act = await repo.get_act(db, uuid.UUID(created.json()["id"]))
        assert act is not None
        pkcs7 = encode_mock_signature(
            document=service._act_package_bytes(act),
            serial=f"SN-{inspector.pinfl}",
            issuer="ISS-1",
            pinfl=inspector.pinfl,
        )
        signed = await inspector_client.post(
            f"{API}/acts/{act.id}/sign",
            json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
        )
        assert signed.status_code == 200, signed.text
        case = await repo.case_for_act(db, act.id)
        assert case is not None
        return case

    first_case = await open_one_case()
    second_case = await open_one_case()
    assert first_case.applicant_id is not None
    assert first_case.applicant_id == second_case.applicant_id  # same violator, two cases

    card = await executor_head_client.get(f"{API}/cases/{second_case.id}")
    assert card.status_code == 200
    body = card.json()
    # No trace of the first case anywhere on the second case's own card: its
    # history/appeals carry only ITS OWN "opened" transition, nothing pointing
    # at the earlier one against the same violator.
    assert str(first_case.id) not in str(body)
    assert [h["to_status"] for h in body["history"]] == ["opened"]

    # The list route has no applicant/permit filter at all — an unsupported
    # query parameter is silently ignored, not rejected, so this reads as "no
    # matching filter exists" rather than "here is every case for X".
    listed = await executor_head_client.get(
        f"{API}/cases", params={"applicant_id": str(first_case.applicant_id)}
    )
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()["items"]}
    assert str(first_case.id) in ids and str(second_case.id) in ids  # filter had no effect
