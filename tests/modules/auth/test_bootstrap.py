"""Bootstrap CLI creates the first sys_admin."""

import uuid

from sqlalchemy import select

from app.bootstrap import bootstrap_admin
from app.db import make_session_factory
from app.modules.auth.models import Role, User


async def test_bootstrap_creates_sys_admin(engine):
    login = f"admin-{uuid.uuid4().hex[:8]}"
    async with make_session_factory(engine)() as db:
        result = await bootstrap_admin(db, login=login, full_name="Boot Admin")
        await db.commit()
    assert result is not None
    assert result.one_time_password and result.otpauth_uri.startswith("otpauth://")
    async with make_session_factory(engine)() as db:
        user = (await db.execute(select(User).where(User.login == login))).scalar_one()
        role = await db.get(Role, user.role_id)
        assert role is not None and role.code == "sys_admin"
        assert user.must_change_password is True
        # cleanup: bootstrap users are committed (not fixture-rolled-back)
        await db.delete(user)
        await db.commit()


async def test_bootstrap_idempotent(engine):
    login = f"admin-{uuid.uuid4().hex[:8]}"
    async with make_session_factory(engine)() as db:
        await bootstrap_admin(db, login=login, full_name="Boot Admin")
        await db.commit()
    async with make_session_factory(engine)() as db:
        again = await bootstrap_admin(db, login=login, full_name="Boot Admin")
        assert again is None  # already exists — no changes
        user = (await db.execute(select(User).where(User.login == login))).scalar_one()
        await db.delete(user)
        await db.commit()
