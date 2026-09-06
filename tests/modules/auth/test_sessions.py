"""Cookie sessions: validation chain, CSRF, idle timeout, revocation, /auth/me, /auth/logout."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core import settings_store
from app.core.security import hash_token, new_token
from app.core.time import business_today
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, Session, User
from tests.conftest import make_client

API = "/api/v1"
ADMIN_ORIGIN = "https://admin.ruxsatnoma.uz"


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
    # Derived from the spec, not a literal: the default idle window is policy and
    # has moved once already (30 -> 300 minutes).
    idle_minutes = settings_store.SETTING_SPECS["session_idle_minutes"].default
    row, token, _ = await make_session(
        db, user, last_seen_at=datetime.now(UTC) - timedelta(minutes=idle_minutes + 1)
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


async def test_user_with_valid_until_yesterday_is_401(db):
    """Same Asia/Tashkent bug as finding 11, one file over: comparing `valid_until`
    (a calendar date) against a UTC `now.date()` kept a fixed-term account (e.g. the
    prosecutor's) alive for up to 5 hours after midnight Tashkent time. "Yesterday"
    is built from business_today() so the test cannot drift from the code it checks."""
    user = await make_user(db, valid_until=business_today() - timedelta(days=1))
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR-AUTH-002"


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


@pytest.fixture
def _cors_allowlist(monkeypatch):
    """Configures the adminka's origin as the sole allowlisted one (ruling 3)."""
    monkeypatch.setenv("CORS_ORIGINS", f'["{ADMIN_ORIGIN}"]')
    # A non-empty cors_origins is the deployed-cross-origin signal the config
    # guard reads (app/config.py, final review of stage 6.6, finding 2) — it
    # refuses the default localhost admin_base_url once that signal is set.
    monkeypatch.setenv("ADMIN_BASE_URL", ADMIN_ORIGIN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_mutation_from_allowlisted_origin_is_accepted(db, _cors_allowlist):
    """The allowlist branch of _origin_allowed, exercised on a real mutating
    request — not just the same-origin fallback the other Origin tests cover."""
    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.post(
            f"{API}/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": ADMIN_ORIGIN}
        )
    assert r.status_code == 204


async def test_mutation_with_body_sourced_csrf_from_allowlisted_origin(db, _cors_allowlist):
    """End-to-end proof of ruling 3's actual point: a cross-origin adminka cannot
    read the csrf_token cookie via JS (host-only, and Set-Cookie is never exposed to
    JS regardless of CORS config), so it must read it from the JSON body instead —
    exactly what a page reload recovery or a fresh mfa/verify response provides."""
    user = await make_user(db)
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        me = await client.get(f"{API}/auth/me", headers={"Origin": ADMIN_ORIGIN})
        csrf = me.json()["csrf_token"]
        r = await client.post(
            f"{API}/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": ADMIN_ORIGIN}
        )
    assert r.status_code == 204
