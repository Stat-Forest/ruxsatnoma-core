"""Login flow: password step, MFA step, lockout, denied audit surviving rollback,
password change.
"""

import uuid

import pyotp
from sqlalchemy import func, select

from app.core.crypto import encrypt_str
from app.core.security import hash_password
from app.db import make_session_factory
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, User
from tests.conftest import make_client

API = "/api/v1"
PASSWORD = "Str0ng!pass"


async def make_staff(db, **overrides) -> tuple[User, str]:
    secret = pyotp.random_base32()
    role_id = (await db.execute(select(Role.id).where(Role.code == "executor_staff"))).scalar_one()
    user = User(
        full_name="Staff",
        role_id=role_id,
        login=f"staff-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password(PASSWORD),
        mfa_secret=encrypt_str(secret),
        **overrides,
    )
    db.add(user)
    await db.flush()
    return user, secret


async def test_full_login_flow(db, engine):
    user, secret = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        assert r1.status_code == 200 and r1.json()["mfa_required"] is True
        code = pyotp.TOTP(secret).now()
        r2 = await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": r1.json()["mfa_token"], "code": code}
        )
        assert r2.status_code == 200
        assert "session" in r2.cookies and "csrf_token" in r2.cookies
        r3 = await client.get(f"{API}/auth/me")
        assert r3.status_code == 200
    async with make_session_factory(engine)() as fresh:
        audits = (
            (
                await fresh.execute(
                    select(AuditLog.action).where(
                        AuditLog.user_id == user.id, AuditLog.result == "success"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "user.login" in audits and "session.create" in audits
        refreshed = await fresh.get(User, user.id)
        assert refreshed is not None
        assert refreshed.last_login_at is not None and refreshed.failed_login_count == 0


async def test_wrong_password_writes_denied_audit(db, engine):
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": "Wrong1!x"}
        )
    assert r.status_code == 401 and r.json()["error"]["code"] == "ERR-AUTH-001"
    async with make_session_factory(engine)() as fresh:
        denied = (
            await fresh.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.user_id == user.id,
                    AuditLog.action == "user.login",
                    AuditLog.result == "denied",
                )
            )
        ).scalar()
        assert denied == 1  # the trail survived the rollback (ruling 2)
        refreshed = await fresh.get(User, user.id)
        assert refreshed is not None
        assert refreshed.failed_login_count == 1


async def test_lockout_after_max_attempts(db, engine):
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        for _ in range(5):
            await client.post(
                f"{API}/auth/login", json={"login": user.login, "password": "Wrong1!x"}
            )
        r = await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
    assert r.status_code == 429 and r.json()["error"]["code"] == "ERR-AUTH-003"
    async with make_session_factory(engine)() as fresh:
        refreshed = await fresh.get(User, user.id)
        assert refreshed is not None and refreshed.locked_until is not None


async def test_unknown_login_is_401_no_user_audit(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/login", json={"login": "ghost", "password": "Wrong1!x"})
    assert r.status_code == 401


async def test_wrong_totp_denied(db):
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        r2 = await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": r1.json()["mfa_token"], "code": "000000"}
        )
    assert r2.status_code == 401


async def test_mfa_token_single_use(db):
    user, secret = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        code = pyotp.TOTP(secret).now()
        token = r1.json()["mfa_token"]
        assert (
            await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": token, "code": code})
        ).status_code == 200
        r3 = await client.post(f"{API}/auth/mfa/verify", json={"mfa_token": token, "code": code})
    assert r3.status_code == 401


async def test_password_change_revokes_other_sessions(db, engine):
    user, secret = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        code = pyotp.TOTP(secret).now()
        await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": r1.json()["mfa_token"], "code": code}
        )
        csrf = client.cookies.get("csrf_token")
        assert csrf is not None
        r = await client.post(
            f"{API}/auth/password/change",
            json={"old_password": PASSWORD, "new_password": "N3w!strongpass"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 204
    async with make_session_factory(engine)() as fresh:
        refreshed = (await fresh.execute(select(User).where(User.id == user.id))).scalar_one()
        from app.core.security import verify_password

        assert refreshed.password_hash is not None
        assert verify_password("N3w!strongpass", refreshed.password_hash)
        assert refreshed.must_change_password is False


async def test_weak_new_password_rejected(db):
    user, secret = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        code = pyotp.TOTP(secret).now()
        await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": r1.json()["mfa_token"], "code": code}
        )
        csrf = client.cookies.get("csrf_token")
        assert csrf is not None
        r = await client.post(
            f"{API}/auth/password/change",
            json={"old_password": PASSWORD, "new_password": "weak"},
            headers={"X-CSRF-Token": csrf},
        )
    assert r.status_code == 422
