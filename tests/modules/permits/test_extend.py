"""Task 8: `POST /permits/{id}/extend` — a holder's own act, never a hodim's
and never an edit of the issued document (`service.extend`'s own docstring).
Since stage 12 (plan 12, R6) the route FILES the extension outright: the
body is `POST /applications`' own, and the row is born SUBMITTED.

Three decisions this file assumes rather than re-derives:

  * **`applications.create` gates the route; `_is_holder` gates the permit.**
    The dependency admits any registered citizen (`other_applicant_client`
    holds it too, being an `applicant`) — it is the SERVICE's own holder
    check that tells `holder_client` and `other_applicant_client` apart, the
    same split `router.py`'s own `POST /applications` makes.
  * **404, not 403, for a non-holder** — `_readable_permit`'s own shape, so
    the route is not a permit-existence oracle.
  * **An EXPIRED permit refuses `extend`, not `active_permit` itself** — the
    fixture stays `active`, and `jobs.expire_permits` (the real nightly
    sweep) is what moves it, so the refusal is proven against a permit that
    reached `expired` the way production ever will.
"""

import uuid
from datetime import timedelta

import pytest

from app.core.errors import DomainError
from app.modules.applications import service as applications_service
from app.modules.applications.schemas import ApplicationFileIn
from app.modules.permits import jobs


def _extension(active_permit, activity_type_id: uuid.UUID, **overrides) -> dict:
    """A priceable filing for next season on the permit's own contour: the
    seeded haymaking tariff needs no norm and no coefficient, so the permits
    suite (which publishes neither) can file it. `on_behalf`/`applicant_id`
    are derived by the service; the body carries the request alone."""
    body = {
        "on_behalf": "self",
        "contour_id": str(active_permit.contour_id),
        "activity_type_id": str(activity_type_id),
        "period_from": "2028-05-01",
        "period_to": "2028-09-30",
        "quantity": "3",
        "rules_accepted": True,
    }
    body.update(overrides)
    return body


async def _extend(signer, permit_id, body):
    return await signer.client.post(
        f"/api/v1/permits/{permit_id}/extend",
        json=body,
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


async def test_the_holder_extends_their_own_permit(
    db, active_permit, holder_client, haymaking_activity_id
) -> None:
    created = await _extend(
        holder_client, active_permit.id, _extension(active_permit, haymaking_activity_id)
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "SUBMITTED"
    assert body["number"].startswith("RX-")
    assert body["kind"] == "extension"
    assert body["parent_application_id"] == str(active_permit.application_id)
    assert body["applicant_id"] == str(active_permit.applicant_id)


async def test_a_stranger_cannot_extend_my_permit(
    active_permit, other_applicant_client, haymaking_activity_id
) -> None:
    refused = await _extend(
        other_applicant_client, active_permit.id, _extension(active_permit, haymaking_activity_id)
    )
    assert refused.status_code == 404  # the same answer the card gives, not an oracle
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_body_naming_another_applicant_is_refused(
    active_permit, holder_client, haymaking_activity_id
) -> None:
    refused = await _extend(
        holder_client,
        active_permit.id,
        _extension(active_permit, haymaking_activity_id, applicant_id=str(uuid.uuid4())),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["reason"] == "applicant_is_not_the_holder"


async def test_an_expired_permit_is_applied_for_afresh(
    db, active_permit, holder_client, haymaking_activity_id, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "business_today", lambda: active_permit.period_to + timedelta(days=1))
    await jobs.expire_permits(db)

    refused = await _extend(
        holder_client, active_permit.id, _extension(active_permit, haymaking_activity_id)
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "not_extendable"


async def test_two_clicks_do_not_file_two_extensions(
    active_permit, holder_client, haymaking_activity_id
) -> None:
    body = _extension(active_permit, haymaking_activity_id)
    first = await _extend(holder_client, active_permit.id, body)
    second = await _extend(holder_client, active_permit.id, {**body, "period_from": "2028-06-01"})
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-APP-002"
    assert second.json()["error"]["details"]["application_id"] == first.json()["id"]


async def test_the_extend_route_requires_an_idempotency_key(
    active_permit, holder_client, haymaking_activity_id
) -> None:
    refused = await holder_client.client.post(
        f"/api/v1/permits/{active_permit.id}/extend",
        json=_extension(active_permit, haymaking_activity_id),
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["reason"] == "idempotency_key_required"


async def test_file_refuses_an_unknown_kind(db, holder_client) -> None:
    """No HTTP route can reach this today: `ApplicationFileIn` forbids a
    `kind` field at all (`extra="forbid"`) and `extend()` always passes its
    own `KIND_EXTENSION` constant. The guard is for the NEXT server caller's
    typo, which must fail closed here rather than reach `flush()` and surface
    as the `kind_valid` CHECK's `IntegrityError` — unhandled, a 500 (lesson).
    """
    with pytest.raises(DomainError) as exc_info:
        await applications_service.file(
            db,
            ApplicationFileIn(on_behalf="self", rules_accepted=True),
            actor=holder_client.user,
            kind="bogus",
        )
    assert exc_info.value.code == "ERR-VAL-001"
    assert exc_info.value.details == {"reason": "unknown_kind"}
