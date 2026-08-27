"""Cookie sessions: validation chain, CSRF, idle timeout, revocation, /auth/me, /auth/logout."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.security import hash_token, new_token
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, Session, User
from tests.conftest import make_client

API = "/api/v1"


async def make_user(db, *, role_code="executor_staff", **overrides) -> User:
    role_id = (await db.execute(select(Role.id).where(Role.code == role_code))).scalar_one()
    user = User(
        full_name="Test User", role_id=role_id, login=f"u-{uuid.uuid4().hex[:8]}", **overrides
    )
    db.add(user)
    await db.flush()
    return user


async def make_session(db, user, **overrides) -> tuple[Session, str, str]:
    token, csrf = new_token(), new_token()
    overrides.setdefault("expires_at", datetime.now(UTC) + timedelta(hours=12))
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=csrf,
        **overrides,
    )
    db.add(row)
    await db.flush()
    return row, token, csrf


async def test_me_with_valid_session(db):
    user = await make_user(db)
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["full_name"] == "Test User"


async def test_me_without_cookie_is_401(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR-AUTH-002"


async def test_expired_session_is_401(db):
    user = await make_user(db)
    _, token, _ = await make_session(db, user, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "ERR-AUTH-002"


async def test_idle_timeout_revokes(db, engine):
    user = await make_user(db)
    row, token, _ = await make_session(
        db, user, last_seen_at=datetime.now(UTC) - timedelta(minutes=31)
    )
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 401
    from app.db import make_session_factory

    async with make_session_factory(engine)() as fresh:
        row2 = await fresh.get(Session, row.id)
        assert row2 is not None and row2.revoked_at is not None


async def test_blocked_user_is_401(db):
    user = await make_user(db, status="blocked")
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 401


async def test_mutation_without_csrf_is_403(db):
    user = await make_user(db)
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.post(f"{API}/auth/logout")
    assert r.status_code == 403 and r.json()["error"]["code"] == "ERR-AUTH-006"


async def test_logout_with_csrf_revokes(db, engine):
    user = await make_user(db)
    row, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        r = await client.post(f"{API}/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    from app.db import make_session_factory

    async with make_session_factory(engine)() as fresh:
        row2 = await fresh.get(Session, row.id)
        assert row2 is not None and row2.revoked_at is not None
        revoke_count = (
            await fresh.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.user_id == user.id,
                    AuditLog.action == "session.revoke",
                    AuditLog.basis == "logout",
                )
            )
        ).scalar()
        assert revoke_count == 1


async def test_must_change_password_gate(db):
    user = await make_user(db, must_change_password=True)
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        me = await client.get(f"{API}/auth/me")  # allowed
        assert me.status_code == 200


async def test_mutation_from_foreign_origin_is_rejected(db):
    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.post(
            f"{API}/auth/logout",
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-AUTH-006"


async def test_mutation_from_own_origin_is_accepted(db):
    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.post(
            f"{API}/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": "http://t"}
        )
    assert r.status_code == 204
