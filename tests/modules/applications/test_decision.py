"""The head's decision — the ERI, the role limit and the two events
(plan 03.9a task 7).

The six tests the brief names are here verbatim. Three more sit below them,
because the brief's `limited_executor_head_client` sets BOTH limits at once and
an over-limit forward proves only that SOMETHING fired:

  * `test_the_amount_limit_alone_forwards` — `max_approve_amount` set,
    `max_approve_area` NULL;
  * `test_the_area_limit_alone_forwards` — the mirror;
  * `test_a_null_limit_is_no_limit_however_large_the_application` — the seeded
    `executor_head` holds NEITHER limit and approves an application whose amount
    and area are both far from zero, which is the whole of decision #29's "a
    NULL limit means no limit";
  * `test_an_unknown_requested_area_is_refused_rather_than_silently_unchecked` —
    `applications.requested_area_ha` is nullable, and a NULL there compared
    against a real `max_approve_area` would silently disable half of decision
    #29 (ruling 22 froze the column at submission precisely so it never is).

`_decide` is the brief's own helper: it fetches `GET /package` and signs exactly
those bytes, which is what controller ruling R5 fixes as the decision's signed
document — the client can only sign bytes it can fetch, and 3.9a serves no
decision-specific ones.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.modules.applications import decision
from app.modules.applications import events as app_events
from app.modules.integrations.adapters.eimzo import encode_mock_signature


async def _decide(client, app_id, route, **body):
    doc = (await client.get(f"/api/v1/applications/{app_id}/package")).content
    return await client.post(
        f"/api/v1/applications/{app_id}/{route}",
        json={
            "pkcs7": encode_mock_signature(
                document=doc, serial="HEAD-1", issuer="ISS-1", pinfl="98765432109876"
            ),
            **body,
        },
    )


async def test_approval_publishes_the_event_3_10_subscribes_to(
    db, executor_head_client, application_in_review
) -> None:
    seen: list[events.Event] = []

    async def handler(session, event) -> None:
        seen.append(event)

    events.subscribe(app_events.APPLICATION_APPROVED, handler)

    result = await _decide(executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    # NOT "APPROVED" — 3.10a's `payments.subscribers.on_application_approved` is
    # registered by `register_event_subscriptions()` and runs INSIDE this
    # request's own transaction, so the invoice is issued and the application is
    # already INVOICED by the time the route serializes its answer. No client
    # ever observes APPROVED (3.10a ruling 14); it exists only in the history.
    assert result.json()["status"] == "INVOICED"

    assert len(seen) == 1
    # `uuid.UUID(...)`, not the raw fixture string: `publish` carries the ORM
    # column's own value, exactly as `submit` and `cancel` already do, and
    # `payments.subscribers._application_id` normalises both shapes for that
    # reason. The brief's snippet compared a `UUID` to a `str`; making the
    # publisher stringify instead would have made this ONE event's payload
    # differ in type from the other three.
    assert seen[0].payload["application_id"] == uuid.UUID(application_in_review)
    assert set(seen[0].payload) == {"application_id"}, (
        "the payload is application_id and NOTHING else — an amount carried on "
        "the event would be a second source of truth for money beside the "
        "stored calculation (frozen in applications/events.py, branch 1)"
    )


async def test_an_over_limit_application_is_forwarded_not_approved(
    db, limited_executor_head_client, application_in_review
) -> None:
    """Decision #29 + ruling 9а: the status does not change, the assignment
    does, and NOTHING is signed. The dangerous alternative was approving it and
    flagging it — 3.10 subscribes to application_approved."""
    seen: list[events.Event] = []

    async def handler(session, event) -> None:
        seen.append(event)

    events.subscribe(app_events.APPLICATION_APPROVED, handler)

    result = await _decide(limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "IN_REVIEW"
    assert body["forwarded_to_organization"] is not None
    assert seen == [], "no approval event may fire for an undecided application"

    timeline = (
        await limited_executor_head_client.get(
            f"/api/v1/applications/{application_in_review}/timeline"
        )
    ).json()
    assert len(timeline["assignments"]) == 2
    assert timeline["signatures"] == [], "nothing is signed on a forward"


async def test_forwarding_with_no_parent_organization_is_a_loud_error(
    agency_executor_head_client, application_in_review_at_agency
) -> None:
    """An escalation that cannot resolve must fail visibly, never silently
    approve or silently stall."""
    result = await _decide(agency_executor_head_client, application_in_review_at_agency, "approve")
    assert result.status_code == 422
    assert "parent" in result.json()["error"]["details"]["reason"]


async def test_rejection_without_grounds_is_refused_before_the_signature(
    executor_head_client, application_in_review
) -> None:
    """tz/04 С8: a refusal carries a legal basis and an RJ-* reason. Checked
    first, so a signature is never spent on a request that cannot succeed."""
    result = await _decide(executor_head_client, application_in_review, "reject")
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "ERR-VAL-001"


async def test_rejection_records_the_reason_and_publishes_its_event(
    db, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    seen: list[events.Event] = []

    async def handler(session, event) -> None:
        seen.append(event)

    events.subscribe(app_events.APPLICATION_REJECTED, handler)

    result = await _decide(
        executor_head_client,
        application_in_review,
        "reject",
        reason_item_id=str(rejection_reason_item.id),
        legal_basis="VMQ 278 п.14",
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "REJECTED"
    assert len(seen) == 1


async def test_a_head_outside_the_zone_cannot_decide(
    other_zone_executor_head_client, application_in_review
) -> None:
    result = await _decide(other_zone_executor_head_client, application_in_review, "approve")
    assert result.status_code in (403, 404)


# --- the half the six above cannot see (decision #29, both axes) -------------


async def test_the_amount_limit_alone_forwards(
    amount_limited_executor_head_client, application_in_review
) -> None:
    """`max_approve_amount` set, `max_approve_area` NULL. The brief's own
    over-limit test sets BOTH, so on its own it cannot say which comparison
    fired — and a role whose area limit is NULL must still be able to trip on
    the amount (lesson: an outcome-only test cannot tell which mechanism
    refused)."""
    result = await _decide(amount_limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "IN_REVIEW"
    assert body["forwarded_to_organization"] is not None


async def test_the_area_limit_alone_forwards(
    area_limited_executor_head_client, application_in_review
) -> None:
    """The mirror: `max_approve_area` set, `max_approve_amount` NULL. This is
    the half of decision #29 that dies quietly if `requested_area_ha` is not
    read — ruling 22 froze the column at submission for exactly this
    comparison."""
    result = await _decide(area_limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "IN_REVIEW"
    assert body["forwarded_to_organization"] is not None


async def test_a_null_limit_is_no_limit_however_large_the_application(
    db: AsyncSession, executor_head_client, application_in_review
) -> None:
    """Decision #29: a NULL limit means NO limit — never zero, which would
    forward every application ever filed and is the failure mode a limit
    implemented with `or Decimal(0)` produces.

    The seeded `executor_head` holds neither limit (migration 0003 leaves both
    columns NULL for all eleven roles), and the application under it is neither
    free nor pointlike — the assertions below pin both, so this test cannot pass
    by approving a zero-amount, zero-area application.
    """
    from sqlalchemy import select

    from app.modules.auth.models import Role

    role = (await db.execute(select(Role).where(Role.code == "executor_head"))).scalar_one()
    assert role.max_approve_amount is None
    assert role.max_approve_area is None

    card = (await executor_head_client.get(f"/api/v1/applications/{application_in_review}")).json()
    assert float(card["calculation"]["amount"]) > 0, "the amount comparison has something to bite"
    assert float(card["requested_area_ha"]) > 0, "and so does the area comparison"

    result = await _decide(executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "INVOICED", "approved, not forwarded"
    assert result.json()["forwarded_to_organization"] is None


async def test_an_unknown_requested_area_is_refused_rather_than_silently_unchecked(
    db: AsyncSession, area_limited_executor_head_client, application_in_review
) -> None:
    """`applications.requested_area_ha` is nullable (ruling 7: a DRAFT is stored
    half-empty), and a NULL compared against a real `max_approve_area` would
    make the comparison a no-op — the head would approve an unlimited area and
    nothing would say so.

    Unreachable through the routes today, because ruling 22 freezes the column
    at submission; asserted anyway, because "unreachable" is a property of the
    current call graph and this guard is what makes half of decision #29 safe if
    that changes.
    """
    from app.modules.applications import repo

    application = await repo.get_application(db, uuid.UUID(application_in_review))
    assert application is not None
    application.requested_area_ha = None
    await db.commit()

    result = await _decide(area_limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 422
    assert result.json()["error"]["details"]["reason"] == "requested_area_unknown"


# --- what the review found implemented but unpinned ---------------------------


async def test_a_zone_free_head_cannot_walk_the_escalation_ladder_by_clicking_twice(
    db: AsyncSession, limited_executor_head_client, application_in_review
) -> None:
    """The forward's replay guard, on exactly the actor it exists for.

    The ZONE does not stop a second click: `service._assert_in_actor_zone`
    returns immediately for an actor whose `Zone` is empty on all three axes, so
    an agency- or republic-level head is unrestricted nationwide. Without
    `_forward`'s own guard the second `POST /approve` re-reads the effective
    organization — now the parent — escalates to the GRANDPARENT, writes a third
    assignment row and a second bogus bounce entry, one rung per click, and the
    leshoz that filed the application can no longer see it.

    What refuses it is «nobody has taken this into work at its current level»:
    `start_review` sets `assigned_user_id`, a forward clears it, so it is
    non-null exactly once per level.
    """
    from app.modules.applications import repo

    first = await _decide(limited_executor_head_client, application_in_review, "approve")
    assert first.status_code == 200, first.text
    parent_id = first.json()["forwarded_to_organization"]
    assert parent_id is not None

    second = await _decide(limited_executor_head_client, application_in_review, "approve")
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "ERR-APP-004"
    assert error["details"]["reason"] == "not_claimed_at_this_level"

    application = await repo.get_application(db, uuid.UUID(application_in_review))
    assert application is not None
    await db.refresh(application)
    assert str(application.assigned_org_id) == parent_id, "still one rung up, not two"
    rows = await repo.list_assignments(db, application.id)
    assert len(rows) == 2, "the second click wrote no third assignment row"
    history = await repo.list_status_history(db, application.id)
    assert [row.reason_text for row in history].count("role_limit_exceeded") == 1


async def test_the_forward_is_visible_on_the_timeline_as_a_bounce_with_its_reason(
    limited_executor_head_client, application_in_review
) -> None:
    """Controller ruling R22: the escalation owes an
    `application_status_history` row with `from_status == to_status ==
    "IN_REVIEW"` — the application genuinely has not moved (ruling 9а), and the
    assignment register alone cannot say WHY it bounced, because
    `application_assignments.reason` is CHECK-constrained to auto/absence/manual
    and carries no free text.

    `reason_text` is a stable TOKEN, never an English sentence: this row is
    citizen-visible, and user-facing wording in this project lives in versioned
    templates an admin owns.
    """
    result = await _decide(limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text

    timeline = (
        await limited_executor_head_client.get(
            f"/api/v1/applications/{application_in_review}/timeline"
        )
    ).json()
    bounces = [
        row
        for row in timeline["status_history"]
        if row["from_status"] == "IN_REVIEW" and row["to_status"] == "IN_REVIEW"
    ]
    assert len(bounces) == 1, "R22's row is the only record of WHY it was escalated"
    assert bounces[0]["reason_text"] == "role_limit_exceeded"
    assert bounces[0]["signatures"] == [], "a bounce is nobody's signed act"
    assert [row["to_status"] for row in timeline["status_history"]] == [
        "DRAFT",
        "SUBMITTED",
        "IN_REVIEW",
        "IN_REVIEW",
    ], "the bounce sits after the transition it did NOT make"


async def test_a_zone_scoped_head_forwards_too(
    db: AsyncSession, zoned_limited_executor_head_client, application_in_review
) -> None:
    """The PRODUCTION shape — `executor_head` with `organization_id` set to its
    own leshoz, which is what migration 0015 assumes. Its zone-free sibling is a
    testing convenience (it can still read the timeline afterwards); this proves
    the forward is not an artefact of that convenience.

    Asserted through `db`, not `GET /timeline`: the forward has moved the
    application into the parent's zone, so this head is now correctly told 404
    on its own escalation — the product question the review is sending to 3.9b.
    """
    from app.modules.applications import repo

    result = await _decide(zoned_limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "IN_REVIEW"
    parent_id = result.json()["forwarded_to_organization"]
    assert parent_id is not None

    application = await repo.get_application(db, uuid.UUID(application_in_review))
    assert application is not None
    await db.refresh(application)
    assert str(application.assigned_org_id) == parent_id
    assert application.assigned_user_id is None

    gone = await zoned_limited_executor_head_client.get(
        f"/api/v1/applications/{application_in_review}/timeline"
    )
    assert gone.status_code == 404, "out of its zone once escalated — 3.9b's product question"


async def test_a_ceiling_equal_to_the_amount_does_not_forward(
    db: AsyncSession, head_with_exact_limits, application_in_review
) -> None:
    """Decision #29 caps what a head may approve, and the comparison is `>`, not
    `>=`. A head whose ceiling is EXACTLY the application's amount and area is
    entitled to decide it — an off-by-one here would escalate every application
    priced at precisely the limit, which is the one value an administrator
    setting that limit is most likely to have chosen deliberately.

    Both sides are asserted, because either alone is satisfiable by an accident:

      * **equal** — the request must REACH `sign()`. It cannot get past it: the
        fixed ERI identity `_decide` presents is bound to
        `executor_head_client`'s user, so this head is refused
        `certificate_owned_by_another_user`. That refusal is the proof — an
        over-limit request never reaches `sign()` at all (ruling 9а), it answers
        200 with a forward;
      * **one tiyin below** — the same head, the same application, and now it
        forwards. Without this half, an implementation that never forwarded
        anything would pass the first half too.
    """
    from app.modules.applications import repo, service

    application = await repo.get_application(db, uuid.UUID(application_in_review))
    assert application is not None
    calculation = await service.current_calculation(db, application.id)
    assert calculation is not None
    area = application.requested_area_ha
    assert area is not None

    async with head_with_exact_limits(max_amount=calculation.amount, max_area=area) as client:
        result = await _decide(client, application_in_review, "approve")
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "ERR-SIGN-001", (
            "equal is not over: the request reached the signature instead of forwarding"
        )

    async with head_with_exact_limits(
        max_amount=calculation.amount - Decimal("0.01"), max_area=area
    ) as client:
        result = await _decide(client, application_in_review, "approve")
        assert result.status_code == 200, result.text
        assert result.json()["forwarded_to_organization"] is not None, (
            "one tiyin over IS over — otherwise the half above proves nothing"
        )


async def test_a_reason_from_another_classifier_is_refused(
    executor_head_client, application_in_review, benefit_category_item_id
) -> None:
    """An existence check is not a validity check (lesson). `classifier_items`
    holds every classifier's values in ONE table, so an id-only check would let
    a benefit category stand as the legal ground for refusing a citizen."""
    result = await _decide(
        executor_head_client,
        application_in_review,
        "reject",
        reason_item_id=str(benefit_category_item_id),
        legal_basis="VMQ 278 п.14",
    )
    assert result.status_code == 422
    error = result.json()["error"]
    assert error["code"] == "ERR-VAL-001"
    assert error["details"]["reason"] == "unknown_rejection_reason"


async def test_an_archived_rejection_reason_is_refused(db: AsyncSession) -> None:
    """The other half: the right classifier, and a row that is no longer in
    force. Nothing in this system is deleted (`status='archived'`), so "it
    exists" and "it may be used" are genuinely different questions.

    Called IN-PROCESS rather than over HTTP, and the row is only `flush`ed so
    the `db` fixture's rollback takes it away again. An HTTP client commits
    before every request (`_commit_pending_before_requests`), and
    `rejection_reasons` is a fixed fifteen-row catalogue that
    `tests/modules/admin/test_admin_seeds.py::test_rejection_reasons_seeded`
    asserts EXACTLY — a private row committed into it fails that test from a
    different package, and an interrupted run strands it forever (lesson: the
    test DB is shared, persistent, and never empty). This is the one guard in
    this file that cannot be proven without writing to a shared catalogue, so it
    does not go over the wire.
    """
    from app.core.errors import DomainError
    from app.modules.admin.models import Classifier, ClassifierItem
    from app.modules.applications import decision as decision_module

    classifier_id = (
        await db.execute(select(Classifier.id).where(Classifier.code == "rejection_reasons"))
    ).scalar_one()
    item = ClassifierItem(
        classifier_id=classifier_id,
        code=f"RJ-TEST-{uuid.uuid4().hex[:8]}",
        name={"uz_cyrl": "Архивланган сабаб", "en": "Archived reason (test)"},
        valid_from=date(2020, 1, 1),
        valid_to=date(2020, 12, 31),
        sort_order=0,
        status="archived",
    )
    db.add(item)
    await db.flush()

    with pytest.raises(DomainError) as raised:
        await decision_module._reason_item(db, item.id)
    assert raised.value.code == "ERR-VAL-001"
    assert raised.value.details == {"reason": "unknown_rejection_reason"}


async def _audit_actions(db: AsyncSession, application_id: str) -> set[str]:
    """Every `audit_log.action` recorded against this application."""
    from sqlalchemy import select

    from app.modules.audit.models import AuditLog

    rows = await db.execute(
        select(AuditLog.action).where(AuditLog.object_id == uuid.UUID(application_id))
    )
    return set(rows.scalars())


# The audit invariant is a hard project rule (CLAUDE.md), and ruling 17 makes it
# sharper: a flow verb audits under its OWN name, never the generic
# `application.status_change`, which belongs to `set_status` and says only that
# a status moved. For an escalation the `audit_log` row is the only record
# besides R22's history row that it happened at all — the status did not change
# and no signature exists to point at.
#
# **The three tests below assert the LITERAL strings, not `decision.APPLICATION_
# APPROVE` and friends.** A test reading the same constant the code writes
# passes for any value of it, including `application.status_change` — verified
# by renaming all three and watching these stay green. The literal is the
# contract: `audit_log.action` is what a `tz/10` reviewer reads years from now,
# and the constant is checked against it separately below.


def test_the_decision_audit_actions_are_the_dotted_verbs_ruling_17_names() -> None:
    """One source of truth, asserted in the direction that catches a rename:
    the constants the code uses must still BE these strings."""
    assert decision.APPLICATION_APPROVE == "application.approve"
    assert decision.APPLICATION_REJECT == "application.reject"
    assert decision.APPLICATION_FORWARD == "application.forward"


async def test_an_approval_audits_under_its_own_action(
    db: AsyncSession, executor_head_client, application_in_review
) -> None:
    result = await _decide(executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    assert "application.approve" in await _audit_actions(db, application_in_review)


async def test_a_rejection_audits_under_its_own_action(
    db: AsyncSession, executor_head_client, application_in_review, rejection_reason_item
) -> None:
    result = await _decide(
        executor_head_client,
        application_in_review,
        "reject",
        reason_item_id=str(rejection_reason_item.id),
        legal_basis="VMQ 278 п.14",
    )
    assert result.status_code == 200, result.text
    assert "application.reject" in await _audit_actions(db, application_in_review)
    assert "application.status_change" not in await _audit_actions(db, application_in_review), (
        "a flow verb never audits under `set_status`'s generic action (ruling 17)"
    )


async def test_a_forward_audits_under_its_own_action_with_the_ceilings_that_fired(
    db: AsyncSession, limited_executor_head_client, application_in_review
) -> None:
    """And the entry carries WHICH ceilings fired and the numbers that fired
    them: an escalation is only auditable if the journal says why this
    application and not the next one."""
    from sqlalchemy import select

    from app.modules.audit.models import AuditLog

    result = await _decide(limited_executor_head_client, application_in_review, "approve")
    assert result.status_code == 200, result.text
    assert "application.forward" in await _audit_actions(db, application_in_review)

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "application.forward",
                AuditLog.object_id == uuid.UUID(application_in_review),
            )
        )
    ).scalar_one()
    assert entry.new_value is not None
    assert set(entry.new_value["over_limit"]) == {"amount", "area"}
    assert entry.new_value["assigned_org_id"] == result.json()["forwarded_to_organization"]
    # The detail lives HERE and not in the citizen-visible `reason_text`
    # (controller minor 4), so both have to be present.
    assert float(entry.new_value["amount"]) > 0
    assert float(entry.new_value["requested_area_ha"]) > 0
