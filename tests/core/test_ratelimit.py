"""Token-bucket per (scope, ip): over-limit → 429 ERR-SYS-006 with retry hint."""

import pytest

from app.core import ratelimit
from app.core.errors import DomainError


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
