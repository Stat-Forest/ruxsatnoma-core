"""audit.service.log(): field mapping, correlation pickup, no autonomous commit."""

import uuid

import structlog

from app.db import make_session_factory
from app.modules.audit import service
from app.modules.audit.models import AuditLog


async def test_log_writes_all_fields(db):
    user_id, object_id = uuid.uuid4(), uuid.uuid4()
    entry = await service.log(
        db,
        action="application.submit",
        user_id=user_id,
        object_type="application",
        object_id=object_id,
        old_value={"status": "DRAFT"},
        new_value={"status": "SUBMITTED"},
        basis="scenario C3",
        result="success",
        ip="10.0.0.1",
        user_agent="pytest",
        correlation_id="req-42",
        extra={"note": "integration test"},
    )
    row = await db.get(AuditLog, entry.id)
    assert row.action == "application.submit"
    assert row.user_id == user_id
    assert row.object_type == "application"
    assert row.object_id == object_id
    assert row.old_value == {"status": "DRAFT"}
    assert row.new_value == {"status": "SUBMITTED"}
    assert row.basis == "scenario C3"
    assert row.result == "success"
    assert str(row.ip) == "10.0.0.1"
    assert row.user_agent == "pytest"
    assert row.correlation_id == "req-42"
    assert row.extra == {"note": "integration test"}
    assert row.occurred_at is not None


async def test_log_picks_correlation_id_from_contextvars(db):
    structlog.contextvars.bind_contextvars(correlation_id="ctx-77")
    try:
        entry = await service.log(db, action="test.ctx")
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id")
    assert entry.correlation_id == "ctx-77"


async def test_log_explicit_correlation_id_wins(db):
    structlog.contextvars.bind_contextvars(correlation_id="ctx-77")
    try:
        entry = await service.log(db, action="test.ctx", correlation_id="explicit-1")
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id")
    assert entry.correlation_id == "explicit-1"


async def test_log_does_not_commit(engine, db):
    entry = await service.log(db, action="test.rollback")
    entry_id = entry.id
    await db.rollback()

    factory = make_session_factory(engine)
    async with factory() as other:
        assert await other.get(AuditLog, entry_id) is None
