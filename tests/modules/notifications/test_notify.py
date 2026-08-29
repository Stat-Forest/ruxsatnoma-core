"""notify() semantics: in-app always, SMS only for a verified phone and an existing
template, no exception ever raised for a missing template (rulings 10 and 11)."""

import uuid

import pytest
from sqlalchemy import select, text

from app.core import settings_store
from app.core.models import SystemSetting
from app.modules.integrations.models import OutboxMessage
from app.modules.notifications import service
from app.modules.notifications.models import Notification
from tests.modules.auth.test_sessions import make_user
from tests.modules.notifications.test_models import make_template

EVENT = "permit.issued"  # seeded with inapp + sms templates by migration 0009


@pytest.fixture(autouse=True)
async def _clean_outbox(db):
    """Same rationale as test_outbox_service.py's fixture of the same name: since
    Task 4's test_channels.py exercises real sms delivery and commits its rows
    outside the `db` fixture's rollback, this file's unscoped destination query
    (test_duplicate_channels_collapse_to_a_single_send) would otherwise also see
    them. Scoped to this file only — test_outbox_service.py keeps its own copy."""
    await db.execute(text("DELETE FROM outbox_messages"))
    await db.commit()


async def _notes(db, user_id) -> list[Notification]:
    return list(
        (await db.execute(select(Notification).where(Notification.recipient_user_id == user_id)))
        .scalars()
        .all()
    )


async def test_inapp_only_when_the_phone_is_not_verified(db):
    user = await make_user(db, phone="998901234567")
    await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "P-1"}
    )
    rows = await _notes(db, user.id)
    assert [r.channel for r in rows] == ["inapp"]
    assert rows[0].status == "delivered"
    assert rows[0].delivered_at is not None
    assert "P-1" in rows[0].rendered_text


async def test_verified_phone_also_gets_an_sms_row_queued_on_the_outbox(db):
    from datetime import UTC, datetime

    user = await make_user(db, phone="998901234567", phone_verified_at=datetime.now(UTC))
    await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "P-2"}
    )
    rows = sorted(await _notes(db, user.id), key=lambda r: r.channel)
    assert [r.channel for r in rows] == ["inapp", "sms"]
    sms = rows[1]
    assert sms.status == "queued"
    assert sms.outbox_message_id is not None
    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    assert message.destination == "sms"
    # The queue carries a reference, never the phone number or the text (ruling 14).
    assert message.payload == {"notification_id": str(sms.id)}


async def test_kill_switch_suppresses_sms_but_never_the_inapp_row(db):
    from datetime import UTC, datetime

    db.add(SystemSetting(key="notifications_sms_enabled", value=False))
    await db.flush()
    settings_store.invalidate("notifications_sms_enabled")
    user = await make_user(db, phone="998901234567", phone_verified_at=datetime.now(UTC))
    await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    assert [r.channel for r in await _notes(db, user.id)] == ["inapp"]
    settings_store.invalidate("notifications_sms_enabled")


async def test_missing_template_still_writes_the_inapp_row(db):
    user = await make_user(db)
    rows = await service.notify(
        db, event_code=f"test.{uuid.uuid4().hex[:8]}", recipient_user_id=user.id, params={"a": 1}
    )
    assert len(rows) == 1
    assert rows[0].channel == "inapp"
    assert rows[0].template_id is None
    assert rows[0].rendered_text  # a fallback body, not an empty string


async def test_renders_in_the_recipients_language(db):
    user = await make_user(db, language="ru")
    await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "P-3"}
    )
    rows = await _notes(db, user.id)
    assert rows[0].language == "ru"
    assert "Разрешение P-3" in rows[0].rendered_text


async def test_blocked_recipient_keeps_the_history_row_but_no_transport(db):
    from datetime import UTC, datetime

    user = await make_user(
        db, phone="998901234567", phone_verified_at=datetime.now(UTC), status="blocked"
    )
    await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    assert [r.channel for r in await _notes(db, user.id)] == ["inapp"]


async def test_email_channel_requires_a_verified_email_and_an_explicit_ask(db):
    from datetime import UTC, datetime

    event = f"test.{uuid.uuid4().hex[:8]}"
    await make_template(db, event_code=event, channel="email", body={"uz_cyrl": "Хат"})
    user = await make_user(db, email=f"{uuid.uuid4().hex[:8]}@example.com")
    await service.notify(db, event_code=event, recipient_user_id=user.id, channels=("email",))
    assert [r.channel for r in await _notes(db, user.id)] == ["inapp"]  # unverified → skipped

    user.email_verified_at = datetime.now(UTC)
    await db.flush()
    await service.notify(db, event_code=event, recipient_user_id=user.id, channels=("email",))
    assert sorted(r.channel for r in await _notes(db, user.id)) == ["email", "inapp", "inapp"]


async def test_unknown_channel_is_a_programming_error(db):
    import pytest

    user = await make_user(db)
    with pytest.raises(ValueError):
        await service.notify(
            db, event_code=EVENT, recipient_user_id=user.id, channels=("telegram",)
        )


async def test_duplicate_channels_collapse_to_a_single_send(db):
    """A repeated channel must never double-send — that would be a real second paid
    SMS to a real phone number, with no error to signal it happened."""
    from datetime import UTC, datetime

    user = await make_user(db, phone="998901234567", phone_verified_at=datetime.now(UTC))
    await service.notify(db, event_code=EVENT, recipient_user_id=user.id, channels=("sms", "sms"))
    rows = await _notes(db, user.id)
    assert sorted(r.channel for r in rows) == ["inapp", "sms"]
    sms = next(r for r in rows if r.channel == "sms")
    assert sms.outbox_message_id is not None
    outbox_rows = (
        (await db.execute(select(OutboxMessage).where(OutboxMessage.destination == "sms")))
        .scalars()
        .all()
    )
    assert len(outbox_rows) == 1
    assert outbox_rows[0].id == sms.outbox_message_id


async def test_unknown_recipient_is_a_programming_error(db):
    import pytest

    with pytest.raises(ValueError):
        await service.notify(db, event_code=EVENT, recipient_user_id=uuid.uuid4(), params={})
