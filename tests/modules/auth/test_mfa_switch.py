"""`mfa_enabled`: the switch that removes the TOTP step from staff login.

Off means the step is GONE, not waived: /auth/login opens the session itself and
mints no handoff token at all, so /auth/mfa/verify has nothing to consume. The
password keeps every one of its guards, and each login taken without a second
factor says so in its own audit row.
"""

import pytest
from sqlalchemy import select, text

from app.core import settings_store
from app.core.models import SystemSetting
from app.db import make_session_factory
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Session, User
from tests.conftest import make_client
from tests.modules.auth.test_login import API, PASSWORD, make_staff

KEY = "mfa_enabled"


async def _set(db, value: bool | None) -> None:
    """Write the override, or remove it entirely with None. The process cache is
    invalidated either way — settings_store holds a value for 60s otherwise."""
    if value is None:
        await db.execute(text("DELETE FROM system_settings WHERE key = :k"), {"k": KEY})
    else:
        await db.merge(SystemSetting(key=KEY, value=value))
    await db.commit()
    settings_store.invalidate(KEY)


@pytest.fixture
async def mfa_off(db):
    """Turn the switch off for one test, then remove the override — a leaked row
    would silently disarm MFA for every later test in the session."""
    await _set(db, False)
    yield
    await _set(db, None)


async def test_default_is_on(db):
    """A fresh database stores no override, so a deployment that never touches
    settings keeps the second factor."""
    assert settings_store.SETTING_SPECS[KEY].default is True
    assert await settings_store.get_bool(db, KEY) is True


async def test_off_logs_in_with_the_password_alone(db, mfa_off):
    """One request, one step: the session cookies ride the /auth/login response
    and the client is authenticated without ever seeing a code field."""
    user, _secret = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
        assert r.status_code == 200
        body = r.json()
        assert body["mfa_required"] is False
        assert body["mfa_token"] is None  # nothing to hand to a second step
        assert body["me"]["user"]["login"] == user.login
        assert "session" in r.cookies and "csrf_token" in r.cookies
        assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_off_mints_no_handoff_token(db, engine, mfa_off):
    """The second step is not merely skipped by the client — it is unreachable.
    An `mfa` OTP row would be a five-minute credential nobody consumes."""
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
        # Even a caller that guesses the old flow gets nowhere.
        r = await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": "anything", "code": "000000"}
        )
        assert r.status_code == 401
    async with make_session_factory(engine)() as fresh:
        rows = (
            await fresh.execute(
                text("SELECT count(*) FROM otp_codes WHERE purpose = 'mfa' AND user_id = :u"),
                {"u": user.id},
            )
        ).scalar_one()
        assert rows == 0


async def test_off_still_needs_the_right_password(db, mfa_off):
    """The switch removes the SECOND factor. If it also let a wrong password
    through, one settings row would be the whole authentication."""
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": "Wrong1!x"}
        )
        assert r.status_code == 401
        assert "session" not in r.cookies


async def test_off_still_refuses_a_locked_account(db, mfa_off):
    """A lockout earned under MFA must survive the switch — otherwise turning it
    off would also release every account the brute-force guard had closed."""
    from datetime import UTC, datetime, timedelta

    user, _ = await make_staff(db)
    user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
        assert r.status_code == 429  # ERR-AUTH-003, the lockout's own code


async def test_off_reaches_a_user_whose_secret_is_gone(db, mfa_off):
    """The case the switch exists for: `mfa_secret` NULL (never enrolled) or
    undecryptable after a secret_key change. With MFA on that user cannot log in
    at all — verify_mfa refuses before it ever looks at the code."""
    user, _ = await make_staff(db)
    user.mfa_secret = None
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
        assert r.status_code == 200 and r.json()["mfa_required"] is False


async def test_off_clears_the_failed_login_counter(db, engine, mfa_off):
    """The shared tail both login paths run: a successful password login resets
    the counter, or the account stays one mistake from a lockout it has already
    cleared."""
    user, _ = await make_staff(db)
    user.failed_login_count = 3
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
    async with make_session_factory(engine)() as fresh:
        refreshed = await fresh.get(User, user.id)
        assert refreshed is not None
        assert refreshed.failed_login_count == 0
        assert refreshed.last_login_at is not None


async def test_off_is_named_in_the_audit_trail(db, engine, mfa_off):
    """Every login taken without a second factor is countable afterwards —
    otherwise the trail cannot answer "who got in while it was off?"."""
    user, _ = await make_staff(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.post(f"{API}/auth/login", json={"login": user.login, "password": PASSWORD})
    async with make_session_factory(engine)() as fresh:
        basis = (
            await fresh.execute(
                select(AuditLog.basis).where(
                    AuditLog.user_id == user.id,
                    AuditLog.action == "user.login",
                    AuditLog.result == "success",
                )
            )
        ).scalar_one()
        assert basis == "mfa disabled"


async def test_back_on_restores_the_code_step(db, engine):
    """Turning it back on restores the second factor for the very next login —
    the 60s process cache must not hold the off value past the flip."""
    user, _ = await make_staff(db)
    await db.commit()
    await _set(db, False)
    await _set(db, None)

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.post(
            f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
        )
        assert r1.json()["mfa_required"] is True
        assert r1.json()["me"] is None  # no session handed out by the password alone
        assert "session" not in r1.cookies
        r2 = await client.post(
            f"{API}/auth/mfa/verify", json={"mfa_token": r1.json()["mfa_token"], "code": "000000"}
        )
        assert r2.status_code == 401
    async with make_session_factory(engine)() as fresh:
        opened = (
            (await fresh.execute(select(Session).where(Session.user_id == user.id))).scalars().all()
        )
        assert opened == []
