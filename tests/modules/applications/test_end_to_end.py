"""Stage 3.9a task 8 — one application walking the whole path, the 3.7
read-narrowing (ruling 11), and the guard that keeps a notification from
going out with no template behind it.

Two things this file deliberately does that an "end-to-end test" usually does
not:

  * it calls the module's PUBLIC SURFACE in process, beside the HTTP walk.
    `service.get`, `service.current_calculation` and `service.set_status` are
    what 3.10a and 3.11a call — an httpx scenario exercises the ROUTES and
    would not notice if one of the three disagreed with them (lesson: "a
    'public surface' task's own end-to-end test can ship the surface
    untested", which is exactly how stage 3.7 shipped `effective_norm` and
    `run_checks` reached by nothing);
  * it asserts the SUBMISSION signature on its own history entry rather than
    at the top level of the timeline. That is the shipped shape (ruling 25):
    top-level `signatures[]` is the DECISION signature and nothing else, and
    each submission attempt carries its own. Both signatures are still proven
    to exist here — only the place each is looked up differs.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.applications import events as app_events
from app.modules.applications import service
from app.modules.notifications.models import NotificationTemplate


async def test_one_application_walks_the_whole_path(
    db: AsyncSession,
    applicant_client,
    hodim_client,
    executor_head_client,
    published_contour,
    published_grazing_norm,
    published_coef_sb,
    grazing_activity_id,
    sheep_type_id,
) -> None:
    """DRAFT -> SUBMITTED -> IN_REVIEW -> APPROVED, with a real ERI at both
    ends, the price from `norms`, the site from `gis`, and the event 3.10
    subscribes to. If this passes, the spine of the process works."""
    from tests.modules.applications.test_submit import _submit

    created = await applicant_client.post("/api/v1/applications", json={"on_behalf": "self"})
    app_id = created.json()["id"]

    await applicant_client.patch(
        f"/api/v1/applications/{app_id}",
        json={
            "activity_type_id": str(grazing_activity_id),
            "contour_id": str(published_contour.id),
            "period_from": "2027-05-01",
            "period_to": "2027-09-30",
            "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
        },
    )

    prechecked = await applicant_client.post(f"/api/v1/applications/{app_id}/precheck")
    assert prechecked.status_code == 200
    assert all(c["result"] != "fail" for c in prechecked.json()["checks"])

    submitted = await _submit(applicant_client, app_id)
    assert submitted.status_code == 200, submitted.text
    number = submitted.json()["number"]

    await hodim_client.post(f"/api/v1/applications/{app_id}/start-review")

    from tests.modules.applications.test_decision import _decide

    approved = await _decide(executor_head_client, app_id, "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "INVOICED", (
        "3.10a's invoice subscriber runs inside the approval's transaction"
    )

    card = (await applicant_client.get(f"/api/v1/applications/{app_id}")).json()
    assert card["number"] == number
    assert card["calculation"]["amount"] is not None
    assert card["calculation"]["rule_version"] is not None

    timeline = (await applicant_client.get(f"/api/v1/applications/{app_id}/timeline")).json()
    assert [e["to_status"] for e in timeline["status_history"]] == [
        "DRAFT",
        "SUBMITTED",
        "IN_REVIEW",
        "APPROVED",
        "INVOICED",
    ]  # APPROVED is in the HISTORY even though no response ever showed it

    # Ruling 25, and the correction to the brief's `len(...) == 2`: the two
    # signatures live at two different levels, and both must be there.
    assert len(timeline["signatures"]) == 1, "the decision is signed, at the top level"
    submitted_entry = next(e for e in timeline["status_history"] if e["to_status"] == "SUBMITTED")
    assert len(submitted_entry["signatures"]) == 1, (
        "the submission is signed on its OWN history entry, not at the top level"
    )
    # Every other entry carries none — the shape 3.9b's several submission
    # attempts extend, one signature per attempt.
    assert [
        len(e["signatures"]) for e in timeline["status_history"] if e["to_status"] != "SUBMITTED"
    ] == [0, 0, 0, 0]

    # --- the same facts, read the way 3.10a and 3.11a read them: IN PROCESS.
    # Nothing above this line touches `service.get` or
    # `service.current_calculation`; the routes reach neither.
    db.expire_all()
    row = await service.get(db, uuid.UUID(app_id))
    assert row is not None
    assert row.number == number
    assert row.status == "INVOICED"

    priced = await service.current_calculation(db, uuid.UUID(app_id))
    assert priced is not None, "3.10a builds its invoice from exactly this"
    assert str(priced.id) == card["calculation"]["id"]
    assert priced.amount == Decimal(card["calculation"]["amount"])
    assert priced.rule_code_version == card["calculation"]["rule_version"]
    assert priced.application_id == uuid.UUID(app_id)


async def test_a_stranger_cannot_read_the_calculation_of_my_application(
    applicant_client, other_applicant_client, submitted_application
) -> None:
    """Ruling 11: the 3.7 carry-over. Until this stage, ANY authenticated user
    could read ANY calculation, because ownership did not exist."""
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    calc_id = card["calculation"]["id"]

    assert (await applicant_client.get(f"/api/v1/calculations/{calc_id}")).status_code == 200
    assert (await other_applicant_client.get(f"/api/v1/calculations/{calc_id}")).status_code == 404


async def test_the_reviewers_of_an_application_read_its_calculation_and_other_staff_do_not(
    applicant_client, hodim_client, other_zone_hodim_client, submitted_application
) -> None:
    """Ruling 11's staff half, and the lesson it rests on: zone scoping is not
    a permission check — a read path needs BOTH. `hodim_client` and
    `other_zone_hodim_client` hold the SAME permission code and differ only in
    the leshoz they are posted to, so a passing pair here can only mean the
    zone was consulted."""
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    calc_id = card["calculation"]["id"]

    assert (await hodim_client.get(f"/api/v1/calculations/{calc_id}")).status_code == 200
    seen_by_stranger = await other_zone_hodim_client.get(f"/api/v1/calculations/{calc_id}")
    assert seen_by_stranger.status_code == 404, seen_by_stranger.text
    assert seen_by_stranger.json()["error"]["code"] == "ERR-SYS-003"


async def test_the_calculation_list_filters_rather_than_refusing(
    applicant_client, other_applicant_client, hodim_client, submitted_application
) -> None:
    """Ruling 11: "the LIST route filters rather than 403s, so a user sees
    their own and nothing else". A stranger naming somebody else's application
    gets an EMPTY page and HTTP 200 — a filter has no way to answer 403, and a
    404 here would make the route an application-existence oracle."""
    mine = await applicant_client.get(
        "/api/v1/calculations", params={"application_id": submitted_application}
    )
    assert mine.status_code == 200, mine.text
    assert mine.json()["total"] == 1

    reviewer = await hodim_client.get(
        "/api/v1/calculations", params={"application_id": submitted_application}
    )
    assert reviewer.status_code == 200, reviewer.text
    assert reviewer.json()["total"] == 1

    stranger = await other_applicant_client.get(
        "/api/v1/calculations", params={"application_id": submitted_application}
    )
    assert stranger.status_code == 200, stranger.text
    assert stranger.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


async def test_the_unfiltered_calculation_list_shows_only_the_callers_own_rows(
    applicant_client, other_applicant_client, submitted_application
) -> None:
    """The same rule with no `application_id` named: an applicant sees the rows
    they created and nothing else. `submit` saves the calculation with the
    applicant as `created_by`, so their own submission is in the list — and the
    other applicant's identical call cannot see it."""
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    calc_id = card["calculation"]["id"]

    mine = await applicant_client.get("/api/v1/calculations")
    assert mine.status_code == 200, mine.text
    assert calc_id in [item["id"] for item in mine.json()["items"]]

    theirs = await other_applicant_client.get("/api/v1/calculations")
    assert theirs.status_code == 200, theirs.text
    assert calc_id not in [item["id"] for item in theirs.json()["items"]]


async def test_set_status_refuses_an_illegal_jump(
    db: AsyncSession, submitted_application: str
) -> None:
    """The method 3.10 and 3.11 move an application with. A jump `tz/05` does
    not allow must be refused HERE, not caught by review three stages later."""
    with pytest.raises(DomainError) as raised:
        await service.set_status(db, uuid.UUID(submitted_application), to_status="PERMIT_ISSUED")
    assert raised.value.code == "ERR-APP-004"


async def test_every_event_this_module_notifies_on_has_a_template(db: AsyncSession) -> None:
    """Ruling 26: with no template, `notify()` writes a raw fallback string
    in-app and sends NOTHING by SMS or e-mail — silently, with one log line.
    The seeded set must cover every event_code the module can emit."""
    assert app_events.NOTIFIED_EVENT_CODES, "the tuple is the registry, never empty"
    for event_code in app_events.NOTIFIED_EVENT_CODES:
        row = await db.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.event_code == event_code,
                NotificationTemplate.channel == "inapp",
                NotificationTemplate.status == "active",
            )
        )
        assert row is not None, f"no active template seeds {event_code}"


def test_every_notify_constant_in_this_module_is_registered_in_the_tuple() -> None:
    """The other half of the guard above, and the one that actually catches the
    NEXT stage's omission: the test above only proves that whatever is IN the
    tuple has a template. A `notify()` call added with a fresh
    `NOTIFY_APPLICATION_*` constant and no tuple entry would sail past it.

    Every dotted event code this module passes to `notify()` is declared as a
    module-level `NOTIFY_*` constant (`service.NOTIFY_APPLICATION_SUBMITTED`,
    `decision.NOTIFY_APPLICATION_APPROVED`/`_REJECTED`), so the two sets must
    be equal — not merely overlap.

    `cancel` is deliberately absent from both: it sends no notification at all
    (no `application.cancelled` template is seeded, and a template-less
    `notify()` writes a raw fallback in-app and sends NOTHING by SMS or
    e-mail), so there is no constant for it either.
    """
    from app.modules.applications import decision

    declared = {
        value
        for module in (service, decision)
        for name, value in vars(module).items()
        if name.startswith("NOTIFY_") and isinstance(value, str)
    }
    assert declared == set(app_events.NOTIFIED_EVENT_CODES), (
        "applications.events.NOTIFIED_EVENT_CODES is the registry every "
        "notification this module sends must be listed in"
    )
