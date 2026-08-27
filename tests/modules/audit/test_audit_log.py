"""audit_log DDL guarantees: append-only triggers and the result CHECK.

Each mutation attempt aborts the transaction, so every case is its own test;
the `db` fixture rolls back, leaving the test DB clean.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.modules.audit.models import AuditLog


async def _insert_row(db) -> AuditLog:
    entry = AuditLog(action="test.write")
    db.add(entry)
    await db.flush()
    return entry


async def test_insert_and_read_back(db):
    entry = await _insert_row(db)
    entry_id = entry.id
    db.expunge_all()
    row = await db.get(AuditLog, entry_id)
    assert row is not None
    assert row.action == "test.write"
    assert row.result == "success"
    assert row.occurred_at is not None and row.occurred_at.tzinfo is not None
    assert row.id.version == 7


async def test_update_forbidden(db):
    entry = await _insert_row(db)
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
            {"id": entry.id},
        )


async def test_delete_forbidden(db):
    entry = await _insert_row(db)
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})


async def test_truncate_forbidden(db):
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("TRUNCATE audit_log"))


async def test_result_check_constraint(db):
    entry = AuditLog(action="test.write", result="hacked")
    db.add(entry)
    with pytest.raises(IntegrityError):
        await db.flush()
