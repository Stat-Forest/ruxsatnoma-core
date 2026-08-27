"""auth DDL: tables, role seeds, audit_log.user_id FK (validated), CHECKs."""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, User


async def test_role_seeds_present(db):
    codes = set((await db.execute(select(Role.code))).scalars())
    assert codes == {
        "sys_admin",
        "central_admin",
        "leadership",
        "executor_staff",
        "gis_specialist",
        "executor_head",
        "chief_forester",
        "inspector",
        "accountant",
        "applicant",
        "prosecutor",
    }
    assert (
        await db.execute(select(func.count()).select_from(Role).where(Role.is_system))
    ).scalar() == 11


async def test_user_insert_minimal(db):
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    user = User(full_name="Test Admin", role_id=role_id)
    db.add(user)
    await db.flush()
    db.expunge_all()
    row = await db.get(User, user.id)
    assert row is not None and row.status == "active"
    assert row.must_change_password is False
    assert row.failed_login_count == 0


async def test_user_pinfl_format_check(db):
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    db.add(User(full_name="Bad", role_id=role_id, pinfl="123"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_user_status_check(db):
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    db.add(User(full_name="Bad", role_id=role_id, status="ghost"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_audit_user_fk_enforced(db):
    db.add(AuditLog(action="test.fk", user_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_audit_user_fk_validated(db):
    convalidated = (
        await db.execute(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'fk_audit_log_user_id_users'"
            )
        )
    ).scalar_one()
    assert convalidated is True
