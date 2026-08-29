"""Per-destination circuit breaker (ruling 13): an outage on one provider must not
burn the retry budget of every queued message, and must not stall other channels."""

import pytest
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


@pytest.fixture
async def _trip_after_one_failure(db):
    """Push outbox_breaker_failures to 1 so a single failure trips the breaker.

    deliver_one() commits internally, so this override durably lands in the
    shared test DB. Cleanup runs in fixture teardown — after yield, not as
    ordinary test-body code — the same idiom tests/core/test_ratelimit.py's
    _low_login_limit/_low_challenge_limit fixtures use for their own
    SystemSetting overrides: pytest runs a fixture's post-yield code even when
    the test body raises, so a genuine assertion failure here still leaves the
    shared dev DB clean instead of poisoning the next run with a leftover
    override and the UniqueViolationError that override would cause.
    """
    key = "outbox_breaker_failures"
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    db.add(SystemSetting(key=key, value=1))
    await db.flush()
    settings_store.invalidate(key)
    yield
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    await db.commit()
    settings_store.invalidate(key)


async def test_a_tripped_destination_is_skipped_while_another_keeps_flowing(
    db, monkeypatch, _trip_after_one_failure
):
    breaker.reset()

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
    breaker.reset()
