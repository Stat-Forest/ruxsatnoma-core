"""Token-bucket per (scope, ip): over-limit → 429 ERR-SYS-006 with retry hint."""

import pytest
from sqlalchemy import delete

from app.core import ratelimit, settings_store
from app.core.errors import DomainError
from app.core.models import SystemSetting
from app.main import create_app
from tests.conftest import make_client

API = "/api/v1"


class _FakeRequest:
    class _Client:
        host = "203.0.113.7"

    client = _Client()


async def test_allows_up_to_limit_then_429(db, monkeypatch):
    dep = ratelimit.rate_limit("test_scope", "ratelimit_otp_per_minute")
    # default limit is 5/minute
    for _ in range(5):
        await dep(_FakeRequest(), db)  # type: ignore[arg-type]
    with pytest.raises(DomainError) as exc:
        await dep(_FakeRequest(), db)  # type: ignore[arg-type]
    assert exc.value.code == "ERR-SYS-006"
    assert exc.value.details is not None
    assert exc.value.details["retry_after_seconds"] >= 1


async def test_scopes_and_ips_are_independent(db):
    dep_a = ratelimit.rate_limit("scope_a", "ratelimit_otp_per_minute")
    dep_b = ratelimit.rate_limit("scope_b", "ratelimit_otp_per_minute")
    for _ in range(5):
        await dep_a(_FakeRequest(), db)  # type: ignore[arg-type]
    # different scope, same ip — its own bucket
    await dep_b(_FakeRequest(), db)  # type: ignore[arg-type]


async def test_refill_over_time(db, monkeypatch):
    dep = ratelimit.rate_limit("refill_scope", "ratelimit_otp_per_minute")
    base = 1000.0
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: base)
    for _ in range(5):
        await dep(_FakeRequest(), db)  # type: ignore[arg-type]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: base + 13.0)  # 5/min → ~1 token
    await dep(_FakeRequest(), db)  # type: ignore[arg-type]


# HTTP-level tests below prove the dependency is actually wired onto the routes
# (not just correct in isolation) — driven through the real app, the same idiom
# as tests/modules/auth. tests/core/conftest.py's autouse `_app_on_test_db`
# fixture already points the app's own db session at the test DB here.


@pytest.fixture
async def _low_login_limit(db):
    """Push /auth/login's per-IP limit down to 2 so a 3rd call trips ERR-SYS-006."""
    key = "ratelimit_login_per_minute"
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    db.add(SystemSetting(key=key, value=2))
    await db.commit()
    settings_store.invalidate(key)
    yield
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    await db.commit()
    settings_store.invalidate(key)


@pytest.fixture
async def _low_challenge_limit(db):
    """Push /auth/eimzo/challenge's per-IP limit down to 2 so a 3rd call trips ERR-SYS-006."""
    key = "ratelimit_challenge_per_minute"
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    db.add(SystemSetting(key=key, value=2))
    await db.commit()
    settings_store.invalidate(key)
    yield
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    await db.commit()
    settings_store.invalidate(key)


async def test_login_route_is_rate_limited(db, _low_login_limit):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        for _ in range(2):
            r = await client.post(
                f"{API}/auth/login", json={"login": "no-such-user", "password": "wrong"}
            )
            assert r.status_code != 429
        r = await client.post(
            f"{API}/auth/login", json={"login": "no-such-user", "password": "wrong"}
        )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ERR-SYS-006"


async def test_eimzo_challenge_route_is_rate_limited(db, _low_challenge_limit):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        for _ in range(2):
            r = await client.post(f"{API}/auth/eimzo/challenge")
            assert r.status_code != 429
        r = await client.post(f"{API}/auth/eimzo/challenge")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ERR-SYS-006"
