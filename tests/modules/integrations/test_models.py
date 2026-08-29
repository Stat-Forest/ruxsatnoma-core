"""0008 DDL: outbox/dead-letter/log tables, idempotency_keys, permission seeds."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.models import IdempotencyKey
from app.db import uuid7
from app.modules.integrations.models import IntegrationLog, OutboxMessage
from tests.modules.auth.test_sessions import make_user


async def test_outbox_defaults_and_status_check(db):
    row = OutboxMessage(destination="sms_otp", payload={"target": "x"})
    db.add(row)
    await db.commit()
    await db.refresh(row)
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_attempt_at is not None

    row.status = "nonsense"
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_outbox_idempotency_key_unique(db):
    key = uuid7()
    db.add(OutboxMessage(destination="d", payload={}, idempotency_key=key))
    await db.commit()
    db.add(OutboxMessage(destination="d", payload={}, idempotency_key=key))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_integration_log_direction_check(db):
    db.add(IntegrationLog(direction="sideways", system="s", endpoint="e"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_integrations_permission_seeds(db):
    rows = await db.execute(
        text(
            "SELECT permission_code FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
            "WHERE r.code = 'central_admin' AND rp.permission_code LIKE 'admin.integrations.%'"
        )
    )
    assert {r[0] for r in rows} == {"admin.integrations.view", "admin.integrations.manage"}


async def test_idempotency_key_composite_pk(db):
    user = await make_user(db)
    key = uuid7()
    db.add(IdempotencyKey(key=key, user_id=user.id, fingerprint="f", route="POST /x"))
    await db.commit()
    db.add(IdempotencyKey(key=key, user_id=user.id, fingerprint="g", route="POST /x"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
