"""alert_dead_outbox (ruling 12): administrators learn about a dead queue in-app,
each dead row is reported exactly once, and the notification it carried is failed."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.db import make_session_factory
from app.modules.audit.models import AuditLog
from app.modules.integrations.models import OutboxMessage
from app.modules.notifications import service as notifications
from app.modules.notifications.models import Notification
from app.workers import jobs
from tests.modules.auth.test_sessions import make_user

EVENT = "permit.issued"


async def test_reports_dead_rows_once_and_fails_their_notifications(db, engine):
    factory = make_session_factory(engine)
    # Stage 3.4's suite permanently seeds some dead outbox rows in this shared
    # test DB, and alert_dead_outbox picks up ANY dead row, not just this test's
    # own — drain whatever is already there first so the == 1 assertion below
    # isn't order-dependent on which other tests already ran.
    await jobs.alert_dead_outbox(factory)

    admin = await make_user(db, role_code="central_admin")
    user = await make_user(db, phone="998901234567", phone_verified_at=datetime.now(UTC))
    rows = await notifications.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    sms = next(r for r in rows if r.channel == "sms")
    message = await db.get(OutboxMessage, sms.outbox_message_id)
    assert message is not None
    message.status = "dead"
    message.last_error = "provider gone"
    await db.commit()

    assert await jobs.alert_dead_outbox(factory) == 1
    assert await jobs.alert_dead_outbox(factory) == 0  # alerted_at makes it idempotent

    await db.refresh(sms)
    await db.refresh(message)
    assert sms.status == "failed"
    assert message.alerted_at is not None

    alerts = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.recipient_user_id == admin.id,
                    Notification.event_code == "system.outbox_dead",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert "sms" in alerts[0].rendered_text
    entry = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "outbox.alert_dead")))
        .scalars()
        .all()
    )
    assert entry and entry[-1].user_id is None


async def test_no_dead_rows_means_no_work(db, engine):
    factory = make_session_factory(engine)
    await jobs.alert_dead_outbox(factory)  # drain anything left by other tests
    assert await jobs.alert_dead_outbox(factory) == 0
