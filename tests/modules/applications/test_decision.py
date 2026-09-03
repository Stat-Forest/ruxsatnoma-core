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

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
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
