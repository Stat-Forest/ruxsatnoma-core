"""Token-bucket per (scope, ip): over-limit → 429 ERR-SYS-006 with retry hint."""

import contextlib

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


# --- 3.11a ruling T5-e: the trim may not reach across scopes ------------------


def _request_from(host: str):
    """A request from one specific address. `_FakeRequest` above is a single
    fixed IP; the eviction tests need many, and the bucket key is
    `(scope, request.client.host)`."""

    class _Request:
        class _Client:
            pass

        client = _Client()

    _Request.client.host = host  # type: ignore[attr-defined]
    return _Request()


async def test_a_flood_on_one_scope_cannot_reset_another_scopes_budget(db, monkeypatch):
    """Task 5 capped `_buckets` to stop an anonymous route minting one dict entry
    per IPv6 source address, but `_buckets` is ONE dict for every scope and the
    trim dropped the least recently touched entries wherever they lived. So an
    attacker who had burned their `/auth/login` budget could get it BACK by
    flooding the anonymous permit-check route from a /64 — 20,000 addresses fits
    inside a single sweep window — and a brute-force throttle that resets on
    demand is not a throttle.

    The per-account `login_max_attempts` lockout in the database is untouched by
    any of this and still applies; it is a different control, on a different key.

    Both halves are asserted: the login bucket keeps its exhausted state, AND the
    flooded scope is still capped — a fix that simply stopped trimming would pass
    the first assertion and reopen the leak the cap exists for.
    """
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 8)
    attacker = "198.51.100.9"
    login = ratelimit.rate_limit("login", "ratelimit_otp_per_minute")  # default 5/minute

    for _ in range(5):
        await login(_request_from(attacker), db)  # type: ignore[arg-type]
    with pytest.raises(DomainError):
        await login(_request_from(attacker), db)  # type: ignore[arg-type]

    # The open door, from a /64 the attacker owns outright.
    flood = ratelimit.rate_limit("public_permit_qr", "ratelimit_otp_per_minute")
    for index in range(64):
        with contextlib.suppress(DomainError):
            await flood(_request_from(f"2001:db8::{index:x}"), db)  # type: ignore[arg-type]

    with pytest.raises(DomainError) as exc:
        await login(_request_from(attacker), db)  # type: ignore[arg-type]
    assert exc.value.code == "ERR-SYS-006"

    flooded = [key for key in ratelimit._buckets if key[0] == "public_permit_qr"]
    assert len(flooded) <= ratelimit.MAX_BUCKETS, "the flooded scope must still be capped"
