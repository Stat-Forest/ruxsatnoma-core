"""The sms/email outbox destinations: a delivery attempt moves the notification to
'sent' with the provider id, and a sender failure leaves the outbox to retry."""

from datetime import UTC, datetime

import pytest

from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.sms import MockSmsSender, get_sms_sender
from app.modules.integrations.models import OutboxMessage
from app.modules.notifications import service
from tests.modules.auth.test_sessions import make_user

EVENT = "permit.issued"


@pytest.fixture(autouse=True)
def _clear_sms_log():
    sender = get_sms_sender()
    if isinstance(sender, MockSmsSender):
        sender.sent.clear()
    yield


async def _verified_user(db):
    return await make_user(db, phone="998901234567", phone_verified_at=datetime.now(UTC))


async def test_delivery_marks_the_notification_sent_and_records_the_provider_id(db):
    user = await _verified_user(db)
    rows = await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "P-7"}
    )
    sms = next(r for r in rows if r.channel == "sms")
    await db.commit()

    while await integrations_service.deliver_one(db):
        pass

    await db.refresh(sms)
    assert sms.status == "sent"
    assert sms.sent_at is not None
    sender = get_sms_sender()
    assert isinstance(sender, MockSmsSender)
    phone, text, reference = sender.sent[-1]
    assert phone == "998901234567"
    assert "P-7" in text
    assert reference == str(sms.id)
    assert sms.provider_message_id == f"mock-{sms.id}"


async def test_a_failing_sender_leaves_the_row_pending_for_retry(db, monkeypatch):
    user = await _verified_user(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    await db.commit()

    async def _boom(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(get_sms_sender(), "send", _boom)
    await integrations_service.deliver_one(db)

    await db.refresh(sms)
    assert sms.status == "queued"  # the transport retries; the record is untouched
    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    assert message.attempts == 1
    assert message.status == "pending"


async def test_a_deleted_notification_does_not_retry_forever(db):
    """A payload pointing at a row that no longer exists must be treated as
    delivered — retrying it would occupy the queue until it goes dead."""
    user = await _verified_user(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    message_id = sms.outbox_message_id
    await db.delete(sms)
    await db.commit()

    await integrations_service.deliver_one(db)

    message = await db.get(OutboxMessage, message_id)
    assert message is not None
    assert message.status == "delivered"


async def test_the_notification_text_never_enters_the_outbox_payload(db):
    user = await _verified_user(db)
    rows = await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "SECRET"}
    )
    sms = next(r for r in rows if r.channel == "sms")

    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    assert "SECRET" not in str(message.payload)
    assert "998901234567" not in str(message.payload)


async def test_unverified_recipient_at_delivery_time_fails_the_row_without_retry(db):
    """The phone can be un-verified between enqueue and delivery. There is nothing
    to retry, so the notification is failed and the transport is considered done."""
    user = await _verified_user(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    user.phone = None
    await db.commit()

    await integrations_service.deliver_one(db)

    await db.refresh(sms)
    assert sms.status == "failed"
    assert sms.error is not None
