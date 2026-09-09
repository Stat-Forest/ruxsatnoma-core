"""Token-bucket per (scope, ip): over-limit → 429 ERR-SYS-006 with retry hint."""

import contextlib
import itertools
from collections import Counter

import pytest
from sqlalchemy import delete

from app.core import ratelimit, settings_store
from app.core.errors import DomainError
from app.core.models import SystemSetting
from app.main import create_app
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

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


# --- Ruling T78-1: POST /eimzo/timestamp owns its own bucket ------------------


@pytest.fixture
async def _low_eimzo_timestamp_limit(db):
    """Push POST /eimzo/timestamp's per-IP limit down to 2 so a 3rd call trips
    ERR-SYS-006."""
    key = "ratelimit_eimzo_timestamp_per_minute"
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    db.add(SystemSetting(key=key, value=2))
    await db.commit()
    settings_store.invalidate(key)
    yield
    await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
    await db.commit()
    settings_store.invalidate(key)


async def test_eimzo_timestamp_route_is_rate_limited(db, _low_eimzo_timestamp_limit):
    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        for _ in range(2):
            r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
            assert r.status_code != 429
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "ERR-SYS-006"


async def test_eimzo_challenge_and_timestamp_do_not_share_a_bucket(db, _low_challenge_limit):
    """The regression ruling T78-1 fixes: the two routes used to share the
    `"eimzo_challenge"` bucket, so exhausting the login-challenge budget from one
    IP (a plausible office-NAT burst) would also 429 an unrelated, in-progress
    document signing through `/eimzo/timestamp`. They must be independent."""
    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        for _ in range(2):
            r = await client.post(f"{API}/auth/eimzo/challenge")
            assert r.status_code != 429
        exhausted = await client.post(f"{API}/auth/eimzo/challenge")
        assert exhausted.status_code == 429

        auth_client(client, token, csrf)
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 200, r.text


# --- 3.11a ruling T5-e: the trim may not reach across scopes ------------------


# The real scope string `permits/public_router.py` charges a scanned QR to —
# `f"public_check:{channel}"` over `service.CHANNEL_QR`. Written out rather than
# imported: `app.core` may not import a domain module, and a test asserting a
# security property of the OPEN route should break loudly if that route ever
# renames its scope, which importing the constant would hide (review).
PUBLIC_SCOPE = "public_check:qr"


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
    flood = ratelimit.rate_limit(PUBLIC_SCOPE, "ratelimit_otp_per_minute")
    for index in range(64):
        with contextlib.suppress(DomainError):
            await flood(_request_from(f"2001:db8::{index:x}"), db)  # type: ignore[arg-type]

    with pytest.raises(DomainError) as exc:
        await login(_request_from(attacker), db)  # type: ignore[arg-type]
    assert exc.value.code == "ERR-SYS-006"

    flooded = [key for key in ratelimit._buckets if key[0] == PUBLIC_SCOPE]
    assert len(flooded) <= ratelimit.MAX_BUCKETS, "the flooded scope must still be capped"


async def test_the_sweep_stops_running_once_every_scope_sits_under_its_own_cap(db, monkeypatch):
    """The gate has to ask about ONE scope, not about the dict.

    With per-scope caps the dict legitimately sits at k scopes x `MAX_BUCKETS`,
    so the old whole-dict test was permanently true in the ordinary post-flood
    steady state — the public scope at its cap plus a single login bucket — and
    every request then ran the full sweep body and freed nothing: an O(n) TTL
    walk plus an O(n) regroup on the event loop, for as long as the state lasted
    (review, Important 2). Three scopes at exactly the cap is that state.

    `_last_sweep` IS the probe: the body's first statement writes it, so a value
    that stops moving means the body stopped running. The clock advances a
    millisecond per call so a re-run would be visible — with a frozen clock every
    sweep would rewrite the same number and prove nothing.
    """
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 8)
    clock = itertools.count(1000.0, 0.001)
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: next(clock))
    ratelimit.reset()

    for scope in ("login", "otp_request", PUBLIC_SCOPE):
        dep = ratelimit.rate_limit(scope, "ratelimit_otp_per_minute")
        for octet in range(8):
            await dep(_request_from(f"10.0.0.{octet}"), db)  # type: ignore[arg-type]

    assert len(ratelimit._buckets) == 24, "well above the old whole-dict gate"
    assert max(ratelimit._scope_counts.values()) == ratelimit.MAX_BUCKETS, (
        "and yet no single scope is OVER its own cap — nothing for the sweep to do"
    )

    settled = ratelimit._last_sweep
    dep = ratelimit.rate_limit(PUBLIC_SCOPE, "ratelimit_otp_per_minute")
    for octet in range(3):
        await dep(_request_from(f"10.0.0.{octet}"), db)  # type: ignore[arg-type]
    assert ratelimit._last_sweep == settled, (
        "the sweep ran again with nothing to free — the 1s throttle is not holding"
    )


async def test_the_scope_counts_stay_in_step_with_the_buckets(db, monkeypatch):
    """`_scope_counts` is what makes the gate O(1), and a count that drifts from
    `_buckets` would either wedge the sweep on forever or switch it off silently.
    Both eviction paths are driven here — the TTL pass and the cap pass — and a
    scope emptied completely must lose its key, not keep a `0` that
    `max(...)` would then read."""
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 4)
    monkeypatch.setattr(ratelimit, "SWEEP_EVERY_SECONDS", 0.0)

    # TTL pass: with a zero idle window every bucket but the caller's own is
    # refilled by definition, so each call empties the rest.
    monkeypatch.setattr(ratelimit, "BUCKET_IDLE_SECONDS", 0.0)
    for scope in ("login", PUBLIC_SCOPE):
        dep = ratelimit.rate_limit(scope, "ratelimit_otp_per_minute")
        for octet in range(6):
            await dep(_request_from(f"10.1.1.{octet}"), db)  # type: ignore[arg-type]
    assert ratelimit._scope_counts == {PUBLIC_SCOPE: 1}
    assert ratelimit._scope_counts == dict(Counter(s for s, _ in ratelimit._buckets))

    # Cap pass: a real idle window, so only the per-scope trim can evict.
    monkeypatch.setattr(ratelimit, "BUCKET_IDLE_SECONDS", 60.0)
    ratelimit.reset()
    flood = ratelimit.rate_limit(PUBLIC_SCOPE, "ratelimit_otp_per_minute")
    for octet in range(10):
        await flood(_request_from(f"10.2.2.{octet}"), db)  # type: ignore[arg-type]
    login = ratelimit.rate_limit("login", "ratelimit_otp_per_minute")
    await login(_request_from("10.3.3.3"), db)  # type: ignore[arg-type]

    assert ratelimit._scope_counts == dict(Counter(s for s, _ in ratelimit._buckets))
    assert ratelimit._scope_counts[PUBLIC_SCOPE] == ratelimit.MAX_BUCKETS
    assert ratelimit._scope_counts["login"] == 1
