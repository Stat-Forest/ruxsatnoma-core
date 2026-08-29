"""Idempotency-Key: replay, fingerprint mismatch, in-flight, stale re-claim."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core import idempotency
from app.core.errors import DomainError
from app.core.idempotency import StoredIdempotentResponse
from tests.modules.auth.test_sessions import make_user


async def test_fresh_then_replay(db):
    user = await make_user(db)
    key = uuid.uuid4()
    ctx = await idempotency.begin(
        db, key=key, user_id=user.id, method="POST", path="/x", body=b'{"a":1}'
    )
    assert ctx.fresh is True
    await ctx.save(db, status_code=201, body={"id": "42"})
    await db.commit()

    with pytest.raises(StoredIdempotentResponse) as exc:
        await idempotency.begin(
            db, key=key, user_id=user.id, method="POST", path="/x", body=b'{"a":1}'
        )
    assert exc.value.status_code == 201 and exc.value.body == {"id": "42"}


async def test_fingerprint_mismatch_409(db):
    user = await make_user(db)
    key = uuid.uuid4()
    await idempotency.begin(db, key=key, user_id=user.id, method="POST", path="/x", body=b"one")
    await db.commit()
    with pytest.raises(DomainError) as exc:
        await idempotency.begin(db, key=key, user_id=user.id, method="POST", path="/x", body=b"two")
    assert exc.value.code == "ERR-SYS-005"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "fingerprint_mismatch"


async def test_in_flight_409_and_stale_reclaim(db):
    user = await make_user(db)
    key = uuid.uuid4()
    await idempotency.begin(db, key=key, user_id=user.id, method="POST", path="/x", body=b"b")
    await db.commit()
    with pytest.raises(DomainError) as exc:
        await idempotency.begin(db, key=key, user_id=user.id, method="POST", path="/x", body=b"b")
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "in_flight"

    # Age the marker past 5 minutes → re-claimable.
    await db.execute(
        text("UPDATE idempotency_keys SET created_at = :t WHERE key = :k"),
        {"t": datetime.now(UTC) - timedelta(minutes=6), "k": str(key)},
    )
    await db.commit()
    ctx = await idempotency.begin(db, key=key, user_id=user.id, method="POST", path="/x", body=b"b")
    assert ctx.fresh is True


async def test_user_namespacing(db):
    u1, u2 = await make_user(db), await make_user(db)
    key = uuid.uuid4()
    c1 = await idempotency.begin(db, key=key, user_id=u1.id, method="POST", path="/x", body=b"b")
    await db.commit()
    c2 = await idempotency.begin(db, key=key, user_id=u2.id, method="POST", path="/x", body=b"b")
    assert c1.fresh and c2.fresh  # same key, different users — independent
