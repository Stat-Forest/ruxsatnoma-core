"""audit.service.log(): field mapping, correlation pickup, no autonomous commit."""

import uuid

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import make_session_factory
from app.modules.audit import service
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, User


async def test_log_writes_all_fields(db):
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    user = User(full_name="Audit Test User", role_id=role_id)
    db.add(user)
    await db.flush()
    user_id, object_id = user.id, uuid.uuid4()
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
    entry_id = entry.id
    db.expunge_all()
    row = await db.get(AuditLog, entry_id)
    assert row.action == "application.submit"
    assert row.user_id == user_id
    assert row.object_type == "application"
    assert row.object_id == object_id
    assert row.old_value == {"status": "DRAFT"}
    assert row.new_value == {"status": "SUBMITTED"}
    assert row.basis == "scenario C3"
    assert row.result == "success"
    assert row.ip == "10.0.0.1"
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


async def test_log_invalid_result_raises_at_call_site(db):
    with pytest.raises(IntegrityError):
        await service.log(db, action="test.invalid", result="hacked")  # type: ignore[arg-type]


async def test_logged_by_matches_only_the_actor_named_in_the_triple(db):
    """`logged_by` is the shared predicate F7 closes with
    (`docs/plans/07.4-findings.md`): `norms.service._forwarded_here_by` calls
    it to answer, without importing `applications` (forbidden, level 2 ->
    level 3), "was THIS actor the one who forwarded THIS application" —
    `applications.decision._forward` writes exactly the row this test builds
    by hand. Three ways to NOT match matter as much as the one way to match:
    a different actor, a different action, and a different object all leave a
    row `already_logged` alone would still find."""
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    forwarder = User(full_name="Forwarding Head", role_id=role_id)
    someone_else = User(full_name="Unrelated Head", role_id=role_id)
    db.add_all([forwarder, someone_else])
    await db.flush()
    application_id = uuid.uuid4()
    other_application_id = uuid.uuid4()
    await service.log(
        db,
        action="application.forward",
        user_id=forwarder.id,
        object_type="application",
        object_id=application_id,
    )

    assert await service.logged_by(
        db,
        action="application.forward",
        object_type="application",
        object_id=application_id,
        user_id=forwarder.id,
    )
    # A DIFFERENT actor asking about the SAME forward is not a match — the
    # whole point of narrowing `already_logged`'s triple to one actor.
    assert not await service.logged_by(
        db,
        action="application.forward",
        object_type="application",
        object_id=application_id,
        user_id=someone_else.id,
    )
    # The right actor, but a DIFFERENT action (e.g. an ordinary approval) —
    # forwarding is not any old touch on the application.
    assert not await service.logged_by(
        db,
        action="application.approve",
        object_type="application",
        object_id=application_id,
        user_id=forwarder.id,
    )
    # The right actor and action, but a DIFFERENT object — a forward of one
    # application must never answer for another.
    assert not await service.logged_by(
        db,
        action="application.forward",
        object_type="application",
        object_id=other_application_id,
        user_id=forwarder.id,
    )
