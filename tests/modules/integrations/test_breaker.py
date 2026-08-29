"""Per-destination circuit breaker (ruling 13): an outage on one provider must not
burn the retry budget of every queued message, and must not stall other channels."""

from sqlalchemy import delete

from app.core import settings_store
from app.core.models import SystemSetting
from app.modules.integrations import breaker, repo, service
from app.modules.integrations.models import OutboxMessage
from app.modules.integrations.senders import SENDERS


def test_trips_after_the_threshold_and_recovers_on_success():
    breaker.reset()
    assert breaker.record_failure("sms", threshold=2, cooldown_seconds=60) is False
    assert breaker.open_destinations() == []
    assert breaker.record_failure("sms", threshold=2, cooldown_seconds=60) is True
    assert breaker.open_destinations() == ["sms"]
    breaker.record_success("sms")
    assert breaker.open_destinations() == []


def test_a_zero_cooldown_leaves_nothing_open():
    breaker.reset()
    breaker.record_failure("sms", threshold=1, cooldown_seconds=0)
    assert breaker.open_destinations() == []


async def test_pick_due_skips_an_open_destination(db):
    db.add(OutboxMessage(destination="sms", payload={}))
    db.add(OutboxMessage(destination="email", payload={}))
    await db.flush()
    row = await repo.pick_due(db, exclude_destinations=["sms"])
    assert row is not None
    assert row.destination == "email"


async def test_a_tripped_destination_is_skipped_while_another_keeps_flowing(db, monkeypatch):
    breaker.reset()
    # deliver_one() commits internally, so this override durably lands in the
    # shared test DB — delete-before/delete-after (as tests/core/test_ratelimit.py
    # does for its own SystemSetting overrides) keeps the test idempotent across
    # repeated runs and leaves no override behind for later tests to trip over.
    await db.execute(delete(SystemSetting).where(SystemSetting.key == "outbox_breaker_failures"))
    db.add(SystemSetting(key="outbox_breaker_failures", value=1))
    await db.flush()
    settings_store.invalidate("outbox_breaker_failures")

    async def _boom(session, payload):
        raise RuntimeError("provider down")

    delivered: list[dict] = []

    async def _ok(session, payload):
        delivered.append(payload)

    monkeypatch.setitem(SENDERS, "sms", _boom)
    monkeypatch.setitem(SENDERS, "email", _ok)
    db.add(OutboxMessage(destination="sms", payload={"n": 1}))
    db.add(OutboxMessage(destination="sms", payload={"n": 2}))
    db.add(OutboxMessage(destination="email", payload={"n": 3}))
    await db.commit()

    while await service.deliver_one(db):
        pass

    assert delivered == [{"n": 3}]  # the healthy channel drained
    assert "sms" in breaker.open_destinations()
    await db.execute(delete(SystemSetting).where(SystemSetting.key == "outbox_breaker_failures"))
    await db.commit()
    settings_store.invalidate("outbox_breaker_failures")
    breaker.reset()
