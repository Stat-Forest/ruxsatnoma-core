"""Outbox service: enqueue atomicity, delivery, backoff, dead, requeue, log."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.errors import DomainError
from app.db import uuid7
from app.modules.integrations import repo, senders, service
from app.modules.integrations.models import InboundDeadLetter, IntegrationLog, OutboxMessage
from tests.modules.auth.test_sessions import make_user


@pytest.fixture(autouse=True)
async def _clean_outbox(db):
    """This suite commits real rows with no transactional rollback (tests/conftest.py's
    `db` fixture only rolls back what is left uncommitted), so earlier runs of
    tests/modules/integrations/test_models.py leave 'pending' rows behind. `pick_due`
    scans the whole table with no per-test scoping — exactly the real worker's job — so
    a stale row would be drained by `deliver_one`/`_drain` here too. Start each test
    with an empty queue."""
    await db.execute(text("DELETE FROM outbox_messages"))
    await db.commit()


@pytest.fixture(autouse=True)
def _test_destination():
    """A controllable sender registered for this module's tests."""
    calls: list[dict] = []

    async def ok_sender(payload: dict) -> None:
        calls.append(payload)

    senders.SENDERS["_test_ok"] = ok_sender
    ok_sender.calls = calls  # type: ignore[attr-defined]
    yield
    senders.SENDERS.pop("_test_ok", None)
    senders.SENDERS.pop("_test_boom", None)


async def _drain(db) -> int:
    n = 0
    while await service.deliver_one(db):
        n += 1
    return n


async def test_enqueue_and_deliver(db):
    msg = await service.enqueue(db, destination="_test_ok", payload={"n": 1})
    assert msg is not None
    msg_id = msg.id  # read before the drain's idle rollback expires `msg` on this shared session
    await db.commit()

    assert await _drain(db) == 1
    row = await db.get(OutboxMessage, msg_id)
    assert row is not None and row.status == "delivered" and row.delivered_at is not None
    sender = senders.SENDERS["_test_ok"]
    assert sender.calls == [{"n": 1}]  # type: ignore[attr-defined]


async def test_enqueue_duplicate_idempotency_key_returns_none(db):
    key = uuid7()
    first = await service.enqueue(db, destination="_test_ok", payload={}, idempotency_key=key)
    await db.commit()
    second = await service.enqueue(db, destination="_test_ok", payload={}, idempotency_key=key)
    await db.commit()
    assert first is not None and second is None


async def test_failed_delivery_backs_off_then_dies(db):
    async def boom(payload: dict) -> None:
        raise RuntimeError("provider down")

    senders.SENDERS["_test_boom"] = boom
    msg = await service.enqueue(db, destination="_test_boom", payload={})
    assert msg is not None
    await db.commit()

    # Attempt 1: retried with next_attempt_at ~1 minute out (base 1 * 2^0).
    assert await service.deliver_one(db) is True
    row = await db.get(OutboxMessage, msg.id)
    assert row is not None
    await db.refresh(row)
    assert row.status == "pending" and row.attempts == 1
    assert row.last_error is not None and "provider down" in row.last_error
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=30)

    # Force due again and exhaust attempts (max 8): goes dead.
    for expected_attempts in range(2, 9):
        await db.execute(
            text("UPDATE outbox_messages SET next_attempt_at = now() WHERE id = :id"),
            {"id": str(msg.id)},
        )
        await db.commit()
        assert await service.deliver_one(db) is True
        await db.refresh(row)
        assert row.attempts == expected_attempts
    assert row.status == "dead"

    # Dead rows are not picked up again.
    assert await service.deliver_one(db) is False


async def test_unknown_destination_goes_dead_immediately(db):
    msg = await service.enqueue(db, destination="_no_such", payload={})
    assert msg is not None
    await db.commit()
    assert await service.deliver_one(db) is True
    row = await db.get(OutboxMessage, msg.id)
    assert row is not None
    await db.refresh(row)
    assert row.status == "dead" and row.attempts == 0


async def test_delivery_writes_integration_log(db):
    msg = await service.enqueue(db, destination="_test_ok", payload={"x": 1})
    assert msg is not None
    msg_id = msg.id  # read before the drain's idle rollback expires `msg` on this shared session
    await db.commit()
    await _drain(db)
    rows = (
        (await db.execute(select(IntegrationLog).where(IntegrationLog.endpoint == str(msg_id))))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].direction == "out" and rows[0].system == "_test_ok"
    assert rows[0].checksum is not None and rows[0].duration_ms is not None


async def test_requeue_resets_dead_row(db):
    user = await make_user(db)
    msg = await service.enqueue(db, destination="_no_such", payload={})
    assert msg is not None
    await db.commit()
    await service.deliver_one(db)  # goes dead

    row = await service.requeue_message(db, msg.id, actor_id=user.id, ip=None)
    await db.commit()
    assert row.status == "pending" and row.attempts == 0

    audit_row = await db.execute(
        text(
            "SELECT action FROM audit_log WHERE action = 'outbox.requeue' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    )
    assert audit_row.scalar_one() == "outbox.requeue"


async def test_requeue_rejects_non_dead(db):
    user = await make_user(db)
    msg = await service.enqueue(db, destination="_test_ok", payload={})
    assert msg is not None
    await db.commit()
    with pytest.raises(DomainError) as exc:
        await service.requeue_message(db, msg.id, actor_id=user.id, ip=None)
    assert exc.value.code == "ERR-VAL-001"


async def test_requeue_unknown_id_raises_not_found(db):
    user = await make_user(db)
    with pytest.raises(DomainError) as exc:
        await service.requeue_message(db, uuid7(), actor_id=user.id, ip=None)
    assert exc.value.code == "ERR-SYS-003"


async def test_discard_dead_letter_marks_processed(db):
    user = await make_user(db)
    letter = InboundDeadLetter(source="eskiz", payload={}, error="schema mismatch")
    db.add(letter)
    await db.commit()

    row = await service.discard_dead_letter(db, letter.id, actor_id=user.id, ip="127.0.0.1")
    await db.commit()
    assert row.status == "discarded"
    assert row.processed_by == user.id and row.processed_at is not None

    audit_row = await db.execute(
        text(
            "SELECT action FROM audit_log WHERE action = 'dead_letter.discard' "
            "ORDER BY occurred_at DESC LIMIT 1"
        )
    )
    assert audit_row.scalar_one() == "dead_letter.discard"


async def test_discard_dead_letter_rejects_non_new(db):
    user = await make_user(db)
    letter = InboundDeadLetter(
        source="eskiz", payload={}, error="schema mismatch", status="discarded"
    )
    db.add(letter)
    await db.commit()

    with pytest.raises(DomainError) as exc:
        await service.discard_dead_letter(db, letter.id, actor_id=user.id, ip=None)
    assert exc.value.code == "ERR-VAL-001"


async def test_discard_dead_letter_unknown_id_raises_not_found(db):
    user = await make_user(db)
    with pytest.raises(DomainError) as exc:
        await service.discard_dead_letter(db, uuid7(), actor_id=user.id, ip=None)
    assert exc.value.code == "ERR-SYS-003"


async def test_list_outbox_filters_by_destination_and_paginates(db):
    unique = f"_test_list_{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        await service.enqueue(db, destination=unique, payload={})
    await db.commit()

    page1, total = await repo.list_outbox(
        db, status="pending", destination=unique, page=1, page_size=2
    )
    assert total == 3
    assert len(page1) == 2
    page2, total2 = await repo.list_outbox(
        db, status="pending", destination=unique, page=2, page_size=2
    )
    assert total2 == 3
    assert len(page2) == 1
    # newest first, no overlap between pages
    assert {row.id for row in page1}.isdisjoint({row.id for row in page2})

    none_status, total_none = await repo.list_outbox(
        db, status="delivered", destination=unique, page=1, page_size=10
    )
    assert none_status == [] and total_none == 0


async def test_get_message_roundtrip_and_missing(db):
    msg = await service.enqueue(db, destination="_test_ok", payload={})
    assert msg is not None
    await db.commit()
    fetched = await repo.get_message(db, msg.id)
    assert fetched is not None and fetched.id == msg.id
    assert await repo.get_message(db, uuid7()) is None


async def test_list_dead_letters_and_get_dead_letter(db):
    letter = InboundDeadLetter(source=f"src-{uuid.uuid4().hex[:8]}", payload={}, error="boom")
    db.add(letter)
    await db.commit()

    rows, _total = await repo.list_dead_letters(db, status="new", page=1, page_size=200)
    assert any(row.id == letter.id for row in rows)
    assert all(row.status == "new" for row in rows)

    fetched = await repo.get_dead_letter(db, letter.id)
    assert fetched is not None and fetched.id == letter.id
    assert await repo.get_dead_letter(db, uuid7()) is None
