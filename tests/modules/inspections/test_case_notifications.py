"""Finding F2 (`plans/07.5-audit-findings.md`): a citizen could be warned,
asked to explain within 5 working days and have a permit revoked over their
head, and learn none of it from the platform they were told to use. Ruling
R1/R2 (`plans/07.6-handover-and-the-violator.md`, decision #138) wires the
four transitions that notify, plus the fail-open resolution for a violator
nobody can reach.

Every test here asserts a NON-EMPTY notification — the shape 07.3/07.5 taught
this project to write, after every defect it found hid data behind a valid
response rather than sending nothing."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from app.core.models import MediaFile
from app.core.time import business_today
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant, Representation, User
from app.modules.gis.models import GisLayer
from app.modules.inspections import events, repo, service
from app.modules.inspections.models import ViolationCase
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.notifications.models import Notification
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.inspections.conftest import unique_pinfl
from tests.modules.inspections.test_violation_cases import opened_case as opened_case

API = "/api/v1/inspections"


def unique_stir() -> str:
    """ASCII digits, matching `applicants.stir`'s CHECK (lesson: `\\d` is
    Unicode-aware in Python, the DB CHECK is not)."""
    return f"{uuid.uuid4().int % 10**9:09d}"


async def _notifications_for(db, user_id: uuid.UUID) -> list[Notification]:
    """Every IN-APP notification for `user_id`, oldest first. `inapp` only —
    the channel `notify()` writes unconditionally (С19), so it is what proves
    "was this event ever raised for this person" without also counting the
    SMS row `_transport_allowed` may or may not have enqueued beside it."""
    rows = await db.execute(
        select(Notification)
        .where(Notification.recipient_user_id == user_id, Notification.channel == "inapp")
        .order_by(Notification.created_at, Notification.id)
    )
    return list(rows.scalars().all())


async def _case_of(db, act_id: uuid.UUID) -> ViolationCase:
    case = await repo.case_for_act(db, act_id)
    assert case is not None
    return case


# --- R1: the four transitions that notify -----------------------------------


async def test_opening_a_case_notifies_the_violator(db, opened_case: dict, applicant_user) -> None:
    rows = await _notifications_for(db, applicant_user.id)
    assert [r.event_code for r in rows] == [events.CASE_OPENED]
    assert opened_case["number"] in rows[0].rendered_text, (
        "a notification that does not name the case is noise"
    )


async def test_requesting_an_explanation_notifies_with_its_deadline(
    db, executor_head_client, opened_case: dict, applicant_user
) -> None:
    resp = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/request-explanation"
    )
    assert resp.status_code == 200, resp.text

    case = await repo.get_case(db, uuid.UUID(opened_case["case_id"]))
    assert case is not None
    rows = await _notifications_for(db, applicant_user.id)
    assert [r.event_code for r in rows] == [events.CASE_OPENED, events.CASE_EXPLANATION_REQUESTED]
    assert str(case.explanation_due_at) in rows[-1].rendered_text


@pytest.mark.parametrize("decision", ["warning", "suspend", "revoke", "transfer"])
async def test_every_decision_reaches_the_violator(
    db, executor_head_client, opened_case: dict, applicant_user, decision: str
) -> None:
    resp = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/decide", json={"decision": decision}
    )
    assert resp.status_code == 200, resp.text

    rows = await _notifications_for(db, applicant_user.id)
    assert rows[-1].event_code == events.CASE_DECIDED
    assert opened_case["number"] in rows[-1].rendered_text


async def test_closing_a_case_notifies_the_violator(
    db, executor_head_client, opened_case: dict, applicant_user
) -> None:
    decided = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/decide", json={"decision": "warning"}
    )
    assert decided.status_code == 200, decided.text

    closed = await executor_head_client.post(f"{API}/cases/{opened_case['case_id']}/close")
    assert closed.status_code == 200, closed.text

    rows = await _notifications_for(db, applicant_user.id)
    assert [r.event_code for r in rows] == [
        events.CASE_OPENED,
        events.CASE_DECIDED,
        events.CASE_CLOSED,
    ]


# --- R1's silent half: the violator's own actions notify nobody -------------


async def test_the_violator_hears_nothing_about_submitting_their_own_explanation(
    db, executor_head_client, applicant_client, opened_case: dict, applicant_user
) -> None:
    requested = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/request-explanation"
    )
    assert requested.status_code == 200, requested.text
    before = len(await _notifications_for(db, applicant_user.id))

    resp = await applicant_client.post(
        f"{API}/cases/{opened_case['case_id']}/explanation", json={"text": "..."}
    )
    assert resp.status_code == 200, resp.text

    assert len(await _notifications_for(db, applicant_user.id)) == before


async def test_the_violator_hears_nothing_about_filing_their_own_appeal(
    db, executor_head_client, applicant_client, opened_case: dict, applicant_user
) -> None:
    decided = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/decide", json={"decision": "warning"}
    )
    assert decided.status_code == 200, decided.text
    before = len(await _notifications_for(db, applicant_user.id))

    appealed = await applicant_client.post(
        f"{API}/cases/{opened_case['case_id']}/appeal", json={"text": "не согласен"}
    )
    assert appealed.status_code == 201, appealed.text

    assert len(await _notifications_for(db, applicant_user.id)) == before


async def test_resolving_the_appeal_notifies_nobody_new(
    db, executor_head_client, applicant_client, opened_case: dict, applicant_user
) -> None:
    """R1: `resolve_appeal` does not re-decide (`decide_case` is the one place
    `decision` is written), so it is one of the three silent transitions."""
    decided = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/decide", json={"decision": "warning"}
    )
    assert decided.status_code == 200, decided.text
    appealed = await applicant_client.post(
        f"{API}/cases/{opened_case['case_id']}/appeal", json={"text": "не согласен"}
    )
    assert appealed.status_code == 201, appealed.text
    before = len(await _notifications_for(db, applicant_user.id))

    resolved = await executor_head_client.post(
        f"{API}/cases/{opened_case['case_id']}/appeal/resolve", json={"result": "upheld"}
    )
    assert resolved.status_code == 200, resolved.text

    assert len(await _notifications_for(db, applicant_user.id)) == before


# --- R2: the legal entity, and the fail-open half ----------------------------


@pytest.fixture
async def representative_user(db) -> User:
    """A registered individual applicant (required by `get_current_user`'s
    `ERR-AUTH-008` gate) who ALSO holds an effective `Representation` over
    `legal_application`'s own applicant — mirrors `tests/modules/applications/
    conftest.py::representative_client`'s own idiom."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    return user


@pytest.fixture
async def legal_application(
    db,
    contours_layer: GisLayer,
    leshoz: Organization,
    approval_doc: MediaFile,
    grazing_activity_id: uuid.UUID,
    representative_user: User,
) -> Application:
    legal = Applicant(kind="legal", stir=unique_stir(), name="OOO Repeat Violator")
    db.add(legal)
    await db.flush()
    db.add(
        Representation(
            applicant_id=legal.id,
            user_id=representative_user.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()

    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    application = Application(
        applicant_id=legal.id,
        submitted_by_user_id=representative_user.id,
        on_behalf="legal",
        activity_type_id=grazing_activity_id,
        contour_id=contour.id,
        contour_version_id=version.id,
        requested_area_ha=Decimal("5.0000"),
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
        status="SUBMITTED",
        channel="portal",
        assigned_org_id=leshoz.id,
    )
    db.add(application)
    await db.flush()
    return application


async def test_a_legal_entity_is_reached_through_its_valid_representative(
    db,
    inspector,
    inspector_client,
    legal_application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
    representative_user: User,
) -> None:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(legal_application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
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

    rows = await _notifications_for(db, representative_user.id)
    assert rows, "the entity's own representative must hear about a case against it"
    assert rows[0].event_code == events.CASE_OPENED


async def test_a_case_still_opens_when_nobody_can_be_notified(
    db, inspector, inspector_client, default_checklist_id: uuid.UUID, vt_01: uuid.UUID
) -> None:
    """R2's fail-open half, and the reason for it: an unreachable applicant
    (here, no applicant at all — an "activity without a permit" act) must not
    be able to block their own violation case by being unreachable."""
    with capture_logs() as logs:
        created = await inspector_client.post(
            f"{API}/acts",
            json={
                "occurred_at": "2027-06-01T10:00:00Z",
                "gps": {"lon": 69.24, "lat": 41.31},
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

    case = await _case_of(db, act_id)
    assert case.applicant_id is None

    warnings = [e for e in logs if e.get("event") == "notification.recipient_unresolved"]
    assert warnings, "expected a notification.recipient_unresolved log line"
    assert warnings[0]["log_level"] == "warning"


# --- the guard ruling #138/R1 exists for ------------------------------------


async def test_every_event_this_module_notifies_on_has_a_template(db) -> None:
    """Without a template, `notify()` writes a raw, untranslated fallback
    string in-app and sends NOTHING by SMS or e-mail — silently, with a
    `notification.template_missing` ERROR log line, forever."""
    from app.modules.notifications.models import NotificationTemplate
    from app.modules.notifications.service import DEFAULT_CHANNELS, SMS_EVENT_CODES

    missing = []
    for event_code in events.NOTIFIED_EVENT_CODES:
        for channel in DEFAULT_CHANNELS:
            # Ruling #211: `sms` is seeded for `SMS_EVENT_CODES` alone.
            if channel == "sms" and event_code not in SMS_EVENT_CODES:
                continue
            row = await db.scalar(
                select(NotificationTemplate).where(
                    NotificationTemplate.event_code == event_code,
                    NotificationTemplate.channel == channel,
                    NotificationTemplate.status == "active",
                )
            )
            if row is None:
                missing.append(f"{event_code}/{channel}")
    assert DEFAULT_CHANNELS, "no default channels — the loop would assert nothing"
    assert not missing, f"no active notification template for: {missing}"


def test_the_notified_set_is_exactly_the_four_transitions_of_ruling_r1() -> None:
    assert set(events.NOTIFIED_EVENT_CODES) == {
        "violation_case.opened",
        "violation_case.explanation_requested",
        "violation_case.decided",
        "violation_case.closed",
    }
