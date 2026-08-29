"""Admin outbox/DLQ API: listing, requeue, discard, permission gates."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.modules.admin.permissions import INTEGRATIONS_MANAGE, INTEGRATIONS_VIEW
from app.modules.integrations import senders, service
from app.modules.integrations.models import InboundDeadLetter
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1"


@pytest.fixture(autouse=True)
async def _clean_outbox(db):
    """Same rationale as test_outbox_service.py's fixture of the same name: this
    suite also commits real rows outside the `db` fixture's rollback (e.g. the
    auth OTP flow enqueues and delivers real `sms_otp` rows from its own tests),
    so an unfiltered listing here would otherwise see leftover rows from earlier
    runs. Scoped to this file only — test_outbox_service.py keeps its own copy."""
    await db.execute(text("DELETE FROM outbox_messages"))
    await db.commit()


async def test_outbox_listing_filters_and_hides_payload(db):
    _, token, csrf = await signed_in_with(db, INTEGRATIONS_VIEW)
    await db.commit()

    delivered_destination = f"_admin_test_ok_{uuid.uuid4().hex[:8]}"
    pending_destination = f"_admin_test_pending_{uuid.uuid4().hex[:8]}"

    async def ok_sender(payload: dict) -> None:
        return None

    senders.SENDERS[delivered_destination] = ok_sender
    try:
        delivered_msg = await service.enqueue(
            db, destination=delivered_destination, payload={"code": "111111"}
        )
        assert delivered_msg is not None
        await db.commit()
        assert await service.deliver_one(db) is True  # the only due row -> delivered

        pending_msg = await service.enqueue(
            db, destination=pending_destination, payload={"code": "222222"}
        )
        assert pending_msg is not None
        await db.commit()
    finally:
        senders.SENDERS.pop(delivered_destination, None)

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        pending_only = await client.get(
            f"{API}/admin/integrations/outbox",
            params={"status": "pending", "destination": pending_destination},
        )
        delivered_excluded = await client.get(
            f"{API}/admin/integrations/outbox",
            params={"status": "pending", "destination": delivered_destination},
        )
    assert pending_only.status_code == 200, pending_only.text
    items = pending_only.json()["items"]
    assert {row["id"] for row in items} == {str(pending_msg.id)}
    assert all("payload" not in row for row in items)
    # the delivered message never shows up under a pending-status filter
    assert delivered_excluded.status_code == 200, delivered_excluded.text
    assert delivered_excluded.json()["items"] == []


async def test_requeue_dead_message(db):
    _, token, csrf = await signed_in_with(db, INTEGRATIONS_MANAGE)
    await db.commit()

    unknown_destination = f"_admin_test_unknown_{uuid.uuid4().hex[:8]}"
    msg = await service.enqueue(db, destination=unknown_destination, payload={"x": 1})
    assert msg is not None
    await db.commit()
    assert await service.deliver_one(db) is True  # unregistered destination -> dead

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        first = await client.post(f"{API}/admin/integrations/outbox/{msg.id}/requeue")
        second = await client.post(f"{API}/admin/integrations/outbox/{msg.id}/requeue")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "pending"
    assert "payload" not in body
    assert second.status_code == 422, second.text
    assert second.json()["error"]["details"]["reason"] == "not_dead"


async def test_discard_dead_letter(db):
    admin, token, csrf = await signed_in_with(db, INTEGRATIONS_MANAGE)
    letter = InboundDeadLetter(
        source=f"eskiz-{uuid.uuid4().hex[:8]}", payload={"raw": "x"}, error="schema mismatch"
    )
    db.add(letter)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        first = await client.post(f"{API}/admin/integrations/dead-letters/{letter.id}/discard")
        second = await client.post(f"{API}/admin/integrations/dead-letters/{letter.id}/discard")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "discarded"
    assert body["processed_by"] == str(admin.id)
    assert "payload" not in body
    assert second.status_code == 422, second.text
    assert second.json()["error"]["details"]["reason"] == "not_new"


async def test_view_permission_cannot_requeue(db):
    _, token, csrf = await signed_in_with(db, INTEGRATIONS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        listing = await client.get(f"{API}/admin/integrations/outbox")
        requeue = await client.post(f"{API}/admin/integrations/outbox/{uuid.uuid4()}/requeue")
    assert listing.status_code == 200, listing.text
    assert requeue.status_code == 403, requeue.text
    assert requeue.json()["error"]["code"] == "ERR-ACL-001"


async def test_inbound_dead_letter_status_check(db):
    """Carried finding from Task 1 (ledger): InboundDeadLetter's CHECK constraint
    had zero direct test coverage before this task."""
    letter = InboundDeadLetter(
        source=f"eskiz-{uuid.uuid4().hex[:8]}", payload={}, error="schema mismatch"
    )
    db.add(letter)
    await db.commit()
    await db.refresh(letter)
    assert letter.status == "new"

    letter.status = "nonsense"
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
