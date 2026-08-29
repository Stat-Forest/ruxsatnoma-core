"""Schema-level guarantees of the notifications tables: exactly one active template
version per (event_code, channel), the status/channel CHECKs, and the SET NULL link
to the outbox row that the 3.4 purge job deletes (ruling 5)."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.integrations.models import OutboxMessage
from app.modules.notifications.models import Notification, NotificationTemplate
from tests.modules.auth.test_sessions import make_user


async def make_template(db, **overrides) -> NotificationTemplate:
    fields = {
        "event_code": f"test.{uuid.uuid4().hex[:8]}",
        "channel": "inapp",
        "body": {"uz_cyrl": "Матн {x}"},
    }
    fields.update(overrides)
    row = NotificationTemplate(**fields)
    db.add(row)
    await db.flush()
    return row


async def test_only_one_active_version_per_event_and_channel(db):
    first = await make_template(db)
    with pytest.raises(IntegrityError):
        await make_template(db, event_code=first.event_code, channel="inapp", version=2)
    await db.rollback()


async def test_archived_version_does_not_block_a_new_active_one(db):
    first = await make_template(db)
    first.status = "archived"
    await db.flush()
    second = await make_template(db, event_code=first.event_code, channel="inapp", version=2)
    assert second.id != first.id


async def test_unknown_channel_is_rejected(db):
    with pytest.raises(IntegrityError):
        await make_template(db, channel="telegram")
    await db.rollback()


async def test_purging_the_outbox_row_keeps_the_notification(db):
    """The 3.4 purge job deletes delivered outbox rows after 7 days; the business
    record must survive its transport (ruling 5)."""
    user = await make_user(db)
    outbox = OutboxMessage(destination="sms", payload={})
    db.add(outbox)
    await db.flush()
    note = Notification(
        recipient_user_id=user.id,
        channel="sms",
        event_code="test.event",
        params={},
        language="uz_cyrl",
        rendered_text="Матн",
        outbox_message_id=outbox.id,
    )
    db.add(note)
    await db.flush()
    await db.delete(outbox)
    await db.flush()
    await db.refresh(note)
    assert note.outbox_message_id is None
    assert note.status == "queued"


async def test_user_language_defaults_to_uz_cyrl_and_rejects_unknown_values(db):
    user = await make_user(db)
    await db.refresh(user)
    assert user.language == "uz_cyrl"
    user.language = "de"
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_outbox_alerted_at_exists_and_starts_null(db):
    row = OutboxMessage(destination="sms", payload={})
    db.add(row)
    await db.flush()
    assert row.alerted_at is None
