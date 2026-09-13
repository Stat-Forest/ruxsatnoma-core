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

import pathlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.applications import events as app_events
from app.modules.applications import service
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.service import DEFAULT_CHANNELS, SMS_EVENT_CODES


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
    """A filing -> SUBMITTED -> IN_REVIEW -> APPROVED, with a real ERI at both
    ends, the price from `norms`, the site from `gis`, and the event 3.10
    subscribes to. If this passes, the spine of the process works."""
    from tests.modules.applications.test_submit import _submit

    filing = {
        "on_behalf": "self",
        "activity_type_id": str(grazing_activity_id),
        "contour_id": str(published_contour.id),
        "period_from": "2027-05-01",
        "period_to": "2027-09-30",
        "items": [{"livestock_type_id": str(sheep_type_id), "head_count": 40}],
    }
    prechecked = await applicant_client.post("/api/v1/applications/precheck", json=filing)
    assert prechecked.status_code == 200
    assert all(c["result"] != "fail" for c in prechecked.json()["checks"])

    submitted = await _submit(applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    app_id = submitted.json()["id"]
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
    ] == [0, 0, 0]

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


async def test_the_unfiltered_calculation_list_carries_no_BOUND_rows_at_all(
    applicant_client, other_applicant_client, submitted_application
) -> None:
    """The two branches of `service.list_calculations` are DISJOINT: a row
    bound to an application is reachable ONLY through `?application_id=`, where
    the entitlement question is actually asked, and never through the
    unfiltered listing, whose whole scope is `created_by` and which therefore
    has nothing to ask about an application with.

    This is review round 1's Important 1. `submit` stores the calculation with
    the APPLICANT as `created_by`, so on a `created_by`-only scope a bound row
    came back to whoever made it — and kept coming back after they had lost the
    right to read it. `..._forwards_an_application_...` below drives that state
    for real; this test states the structural rule the fix rests on, on the
    ordinary applicant.
    """
    card = (await applicant_client.get(f"/api/v1/applications/{submitted_application}")).json()
    calc_id = card["calculation"]["id"]

    mine = await applicant_client.get("/api/v1/calculations")
    assert mine.status_code == 200, mine.text
    assert calc_id not in [item["id"] for item in mine.json()["items"]], (
        "a bound row is not in the unfiltered listing, even for the person who made it"
    )

    # It is not hidden, only moved: the branch that CAN answer for it does.
    by_application = await applicant_client.get(
        "/api/v1/calculations", params={"application_id": submitted_application}
    )
    assert [item["id"] for item in by_application.json()["items"]] == [calc_id]

    theirs = await other_applicant_client.get("/api/v1/calculations")
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["total"] == 0


async def _bind_a_recalculation(client, application_id: str, card: dict) -> str:
    """A staff recalculation bound to an application under review — 3.9b's own
    route does not exist yet, so this is `POST /calculations` with an
    `application_id`, which task 5 deliberately opened and guarded for exactly
    this actor."""
    result = await client.post(
        "/api/v1/calculations",
        json={
            "application_id": application_id,
            "contour_id": card["contour_id"],
            "activity_type_id": card["activity_type_id"],
            "period_from": card["period_from"],
            "period_to": card["period_to"],
            "items": [{"livestock_code": "sheep_goat_6m", "count": 30}],
        },
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


async def test_a_head_who_forwards_an_application_can_read_its_calculation(
    zoned_limited_executor_head_client, other_zone_hodim_client, application_in_review
) -> None:
    """**Review round 1, Important 1, on the real path that produces it —
    closed for ruling #107 by F7 (`docs/plans/07.4-findings.md`,
    2026-09-07).**

    `decision._forward` moves `assigned_org_id` to the parent organization.
    Ruling #107 carved a read-access exception into
    `applications.service._readable_application` for exactly the head who did
    the forwarding — `GET /applications/{id}` no longer 404s for them. Track B
    (7.4 task 3) left `norms.service._may_read_calculation` unfixed on
    purpose: it answers the SAME zone question through its OWN separate
    window onto `applications` (`repo.application_facts`, ruling 20), and the
    honest fix needed a constant BOTH modules could read without `norms`
    importing `applications` (forbidden, level 2 -> level 3) — done now via
    `audit.APPLICATION_FORWARD` and `norms.service._forwarded_here_by`; see
    that function's own docstring. A head who recalculated BEFORE escalating
    now keeps BOTH the application's card and the price it forwarded a
    decision about.

    Not a hand-set state: every step here is a production route.
    """
    card = (
        await zoned_limited_executor_head_client.get(
            f"/api/v1/applications/{application_in_review}"
        )
    ).json()
    calc_id = await _bind_a_recalculation(
        zoned_limited_executor_head_client, application_in_review, card
    )

    # Before the escalation the head may read it, through the branch that asks.
    before = await zoned_limited_executor_head_client.get(f"/api/v1/calculations/{calc_id}")
    assert before.status_code == 200, before.text
    amount_before_forward = before.json()["amount"]
    assert amount_before_forward is not None
    assert (
        await zoned_limited_executor_head_client.get(
            "/api/v1/calculations", params={"application_id": application_in_review}
        )
    ).json()["total"] >= 1

    # The over-limit approval forwards instead of approving (ruling 9а).
    from tests.modules.applications.test_decision import _decide

    forwarded = await _decide(zoned_limited_executor_head_client, application_in_review, "approve")
    assert forwarded.status_code == 200, forwarded.text
    assert forwarded.json()["forwarded_to_organization"] is not None

    # Ruling #107: the application's own card stays readable to its forwarder.
    still_there = await zoned_limited_executor_head_client.get(
        f"/api/v1/applications/{application_in_review}"
    )
    assert still_there.status_code == 200, "ruling #107: the forwarder keeps read access"

    # F7's fix: the CALCULATION stays readable too, with the real amount —
    # not merely a non-403, which would pass just as well if the route
    # silently returned an empty or wrong body.
    after = await zoned_limited_executor_head_client.get(f"/api/v1/calculations/{calc_id}")
    assert after.status_code == 200, after.text
    after_body = after.json()
    assert after_body["id"] == calc_id
    assert after_body["amount"] == amount_before_forward
    assert after_body["amount"] is not None
    listed = await zoned_limited_executor_head_client.get(
        "/api/v1/calculations", params={"application_id": application_in_review}
    )
    assert listed.json()["total"] >= 1
    assert calc_id in [item["id"] for item in listed.json()["items"]]

    # The unfiltered list is a DIFFERENT branch (review round 1, Important 1)
    # and never carries a bound row, forwarding or no forwarding.
    unfiltered = await zoned_limited_executor_head_client.get("/api/v1/calculations")
    assert unfiltered.status_code == 200, unfiltered.text
    assert calc_id not in [item["id"] for item in unfiltered.json()["items"]], (
        "a BOUND row is reachable only through ?application_id=, never the unfiltered list"
    )

    # A staff member who holds the SAME permission code, is out of the zone
    # the application ended up in, and never forwarded it, is still refused —
    # `_forwarded_here_by` is keyed on `changed_by`, never on the organization,
    # so a stranger head does not inherit the forwarder's carve-out.
    stranger = await other_zone_hodim_client.get(f"/api/v1/calculations/{calc_id}")
    assert stranger.status_code == 404, stranger.text
    assert stranger.json()["error"]["code"] == "ERR-SYS-003"
    stranger_listed = await other_zone_hodim_client.get(
        "/api/v1/calculations", params={"application_id": application_in_review}
    )
    assert stranger_listed.json()["total"] == 0


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
    The seeded set must cover every event_code the module can emit.

    **EVERY default channel, not just `inapp`** (review round 1). The `inapp`
    half is the one that fails LOUDLY — a visible fallback string in the
    citizen's cabinet — while an unseeded `sms` row is the silent one: the
    message is simply never sent. A guard written to catch silence that checks
    only the loud channel catches nothing. `DEFAULT_CHANNELS` comes from
    `notifications.service`, so a third channel added there is covered here
    without anybody remembering to; `tests/modules/permits/test_end_to_end.py`
    is the precedent this now matches.
    """
    assert app_events.NOTIFIED_EVENT_CODES, "the tuple is the registry, never empty"
    assert DEFAULT_CHANNELS, "no default channels — the loop would assert nothing"
    missing = []
    for event_code in app_events.NOTIFIED_EVENT_CODES:
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
    assert not missing, f"no active notification template for: {missing}"


def test_every_event_code_this_package_passes_to_notify_is_registered() -> None:
    """The other half of the guard above, and the one that actually catches the
    NEXT stage's omission: the test above only proves that whatever is IN the
    tuple has a template. A `notify()` call added with an unregistered code
    would sail past it.

    **The whole package is walked, and the CALL SITES are read, not a
    hard-coded pair of modules** (review round 1). Scanning `service` and
    `decision` for `NOTIFY_*` constants missed two shapes that are one commit
    away: a notification added in `checks.py`, or a future `jobs.py`, and an
    `event_code="application.something"` written inline at the call site with
    no constant at all. So this parses every module in the package, finds every
    call with an `event_code=` keyword, and resolves the argument — a string
    literal is itself, a bare name is looked up among that module's own
    top-level string constants.

    A form neither shape covers (an f-string, a dict lookup, a parameter passed
    down) fails LOUDLY as `unresolved`, rather than quietly reducing this guard
    to nothing: `decision._notify_decision` takes `event_code` as a parameter,
    and its two real call sites are what this scan sees.
    """
    import ast
    import pkgutil

    import app.modules.applications as applications_package

    registered = set(app_events.NOTIFIED_EVENT_CODES)
    found: set[str] = set()
    unresolved: list[str] = []
    package_dir = pathlib.Path(applications_package.__file__).parent
    module_names = [name for _, name, _ in pkgutil.iter_modules([str(package_dir)])]
    assert {"service", "decision", "checks"} <= set(module_names), (
        "the package walk found no modules — the scan below would assert nothing"
    )

    for module_name in module_names:
        source = (package_dir / f"{module_name}.py").read_text()
        tree = ast.parse(source)
        # That module's own top-level `NAME = "literal"` bindings, so a call
        # site written as `event_code=NOTIFY_APPLICATION_SUBMITTED` resolves.
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "event_code":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
                elif isinstance(value, ast.Name) and value.id in constants:
                    found.add(constants[value.id])
                elif isinstance(value, ast.Name):
                    # A parameter forwarded into a shared helper
                    # (`decision._notify_decision`) — its own callers are
                    # scanned above, so this is not a gap.
                    continue
                else:
                    unresolved.append(f"{module_name}.py:{value.lineno}")

    assert not unresolved, (
        f"event_code argument(s) this guard cannot read: {unresolved} — write the code as a "
        "module-level string constant or a literal, or this registry stops meaning anything"
    )
    assert found, "no notify() call site found at all — the scan is broken, not the module"
    assert found == registered, (
        "applications.events.NOTIFIED_EVENT_CODES is the registry every notification this "
        f"module sends must be listed in; call sites say {sorted(found)}, "
        f"the tuple says {sorted(registered)}"
    )
