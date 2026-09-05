"""Task 8: `POST /permits/{id}/extend` — a holder's own act, never a hodim's
and never an edit of the issued document (`service.extend`'s own docstring).

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

from datetime import timedelta

import pytest

from app.core.errors import DomainError
from app.modules.applications import service as applications_service
from app.modules.applications.schemas import ApplicationCreate
from app.modules.permits import jobs


async def test_the_holder_extends_their_own_permit(db, active_permit, holder_client) -> None:
    created = await holder_client.client.post(f"/api/v1/permits/{active_permit.id}/extend")
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "DRAFT"
    assert body["kind"] == "extension"
    assert body["parent_application_id"] == str(active_permit.application_id)


async def test_a_stranger_cannot_extend_my_permit(active_permit, other_applicant_client) -> None:
    refused = await other_applicant_client.client.post(f"/api/v1/permits/{active_permit.id}/extend")
    assert refused.status_code == 404  # the same answer the card gives, not an oracle
    assert refused.json()["error"]["code"] == "ERR-SYS-003"


async def test_an_expired_permit_is_applied_for_afresh(
    db, active_permit, holder_client, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "business_today", lambda: active_permit.period_to + timedelta(days=1))
    await jobs.expire_permits(db)
    refused = await holder_client.client.post(f"/api/v1/permits/{active_permit.id}/extend")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["reason"] == "not_extendable"


async def test_two_clicks_do_not_file_two_extensions(active_permit, holder_client) -> None:
    first = await holder_client.client.post(f"/api/v1/permits/{active_permit.id}/extend")
    second = await holder_client.client.post(f"/api/v1/permits/{active_permit.id}/extend")
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "ERR-APP-002"
    assert second.json()["error"]["details"]["application_id"] == first.json()["id"]


async def test_create_draft_refuses_an_unknown_kind(db, holder_client) -> None:
    """No HTTP route can reach this today: `ApplicationCreate` forbids a
    `kind` field at all (`extra="forbid"`) and `extend()` always passes its
    own `KIND_EXTENSION` constant. The guard is for the NEXT server caller's
    typo, which must fail closed here rather than reach `flush()` and surface
    as the `kind_valid` CHECK's `IntegrityError` — unhandled, a 500 (lesson).
    """
    with pytest.raises(DomainError) as exc_info:
        await applications_service.create_draft(
            db,
            ApplicationCreate(on_behalf="self"),
            actor=holder_client.user,
            kind="bogus",
        )
    assert exc_info.value.code == "ERR-VAL-001"
    assert exc_info.value.details == {"reason": "unknown_kind"}
