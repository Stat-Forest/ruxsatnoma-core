"""The sms/email outbox destinations: a delivery attempt moves the notification to
'sent' with the provider id, and a sender failure leaves the outbox to retry."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core import settings_store
from app.core.models import SystemSetting
from app.modules.integrations import breaker
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.email import MockEmailSender, get_email_sender
from app.modules.integrations.adapters.sms import MockSmsSender, get_sms_sender
from app.modules.integrations.models import OutboxMessage
from app.modules.notifications import service
from tests.modules.auth.test_sessions import make_user
from tests.modules.notifications.test_models import make_template

EVENT = "permit.issued"


@pytest.fixture(autouse=True)
async def _clean_outbox(db):
    """Same rationale as test_outbox_service.py's fixture of the same name — but
    this file is the *source* of the leak, not just a target: it is the first in
    the suite to commit real sms/email rows (delivery has to commit — deliver_one
    always commits the outcome). Without this guard, a stray 'pending' row left by
    an earlier file could be the one `pick_due` claims instead of the row a test
    just created, and this file's own rows would otherwise reach later files — as
    they did in test_notify.py before that file got its own copy of this fixture.
    Scoped to this file only — test_outbox_service.py keeps its own copy."""
    await db.execute(text("DELETE FROM outbox_messages"))
    await db.commit()


@pytest.fixture(autouse=True)
def _clear_sms_log():
    sender = get_sms_sender()
    if isinstance(sender, MockSmsSender):
        sender.sent.clear()
    yield


async def _verified_user(db):
    return await make_user(
        db, role_code="applicant", phone="998901234567", phone_verified_at=datetime.now(UTC)
    )


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
    # ...and the transport is DONE: unlike the kill switch below, no retry could
    # ever conjure a verified phone, so the outbox row must not stay in the queue.
    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    assert message.status == "delivered"


async def test_email_delivery_marks_the_notification_sent_and_records_the_provider_id(db):
    """Mirrors the sms case above; the mock SMTP client returns no provider id at
    all (unlike Eskiz), so `provider_message_id` must end up None, not a string."""
    event = f"test.email.{uuid.uuid4().hex[:8]}"
    await make_template(
        db,
        event_code=event,
        channel="email",
        subject={"uz_cyrl": "Рухсатнома {permit_number}"},
        body={"uz_cyrl": "Рухсатнома {permit_number} расмийлаштирилди."},
    )
    email_address = f"{uuid.uuid4().hex[:8]}@example.com"  # compared after commits expire `user`
    user = await make_user(db, email=email_address, email_verified_at=datetime.now(UTC))
    rows = await service.notify(
        db,
        event_code=event,
        recipient_user_id=user.id,
        channels=("email",),
        params={"permit_number": "P-9"},
    )
    email_row = next(r for r in rows if r.channel == "email")
    await db.commit()

    while await integrations_service.deliver_one(db):
        pass

    await db.refresh(email_row)
    assert email_row.status == "sent"
    assert email_row.sent_at is not None
    sender = get_email_sender()
    assert isinstance(sender, MockEmailSender)
    to, subject, text_body = sender.sent[-1]
    assert to == email_address
    assert subject == email_row.subject
    assert "P-9" in text_body
    assert email_row.provider_message_id is None


async def test_the_kill_switch_pauses_the_queue_instead_of_destroying_it(db):
    """`notifications_sms_enabled` is an ops switch whose documented purpose is a
    spent SMS balance. Flipping it off must PAUSE delivery — the outbox's backoff
    ladder rides the pause out — not permanently fail every queued message with a
    reason blaming the recipient (final whole-branch review of 3.5, finding 2)."""
    user = await _verified_user(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    await db.commit()

    db.add(SystemSetting(key="notifications_sms_enabled", value=False))
    await db.flush()
    settings_store.invalidate("notifications_sms_enabled")
    try:
        await integrations_service.deliver_one(db)
    finally:
        await db.execute(
            text("DELETE FROM system_settings WHERE key = 'notifications_sms_enabled'")
        )
        await db.commit()
        settings_store.invalidate("notifications_sms_enabled")
        breaker.reset()

    await db.refresh(sms)
    assert sms.status == "queued"  # nothing destroyed, nothing mislabelled
    assert sms.error is None
    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    assert message.status == "pending"  # retryable: the switch may come back on
    assert message.attempts == 1
    # An admin reading last_error must see an operator switch, not a provider fault,
    # and no personal data (this column is admin-visible AND logged).
    assert "disabled" in (message.last_error or "")
    assert "998901234567" not in (message.last_error or "")
