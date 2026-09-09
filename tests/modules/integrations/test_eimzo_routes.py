"""Task 7 (plan 05.2, rulings R2/R5): the two proxy routes a browser genuinely
needs from the E-IMZO server, which itself sits on the stack's private
network and is never reachable from the internet directly.

`POST /eimzo/timestamp` is authenticated (any signed-in user) and rate-limited
through the same `rate_limit` mechanism `/auth/eimzo/challenge` uses -- but its
OWN scope and settings key (`"eimzo_timestamp"` /
`ratelimit_eimzo_timestamp_per_minute`), never the challenge route's own
bucket (ruling T78-1: sharing it would let an anonymous login-challenge burst
throttle unrelated, in-progress document signing behind the same NAT). The
bucket-isolation tests live in `tests/core/test_ratelimit.py`, beside
`/auth/eimzo/challenge`'s own. `GET /eimzo/health` is `sys_admin` only,
reachable through the superuser bypass alone
(`integrations.permissions.EIMZO_HEALTH` is registered but granted to
nobody, the same shape `applications.assign` uses).
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.eimzo import EimzoError
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"

CLIENT_IP = "203.0.113.77"


class _OutageAdapter:
    """No `.calls` attribute -- `_log_eimzo_calls`'s `getattr(..., ())` fallback,
    the same shape `test_eimzo_provider_outage.py`'s own fakes use."""

    async def attach_timestamp(self, pkcs7: str, ip: str | None = None) -> str:
        raise EimzoError("ERR-INT-001")

    async def health(self) -> dict[str, Any]:
        raise EimzoError("ERR-INT-001")


class _RefusingAdapter:
    async def attach_timestamp(self, pkcs7: str, ip: str | None = None) -> str:
        raise EimzoError("ERR-INT-002", provider_status=-11, reason="certificate_invalid")

    async def health(self) -> dict[str, Any]:
        raise EimzoError("ERR-INT-002", provider_status=-11, reason="certificate_invalid")


class _CapturingAdapter:
    def __init__(self) -> None:
        self.captured_ip: str | None = None

    async def attach_timestamp(self, pkcs7: str, ip: str | None = None) -> str:
        self.captured_ip = ip
        return pkcs7


async def test_timestamp_route_refuses_an_unauthenticated_caller(db: AsyncSession) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 401


async def test_a_signed_in_user_gets_the_timestamped_pkcs7_back(db: AsyncSession) -> None:
    """Default test settings run `eimzo_mode=mock` -- `MockEimzo.attach_timestamp`
    echoes its input unchanged (its own docstring), which is exactly what proves
    the route reached the adapter rather than short-circuiting somewhere above it."""
    user = await make_user(db)  # executor_staff, no grants: ANY authenticated user qualifies
    _, token, csrf = await make_session(db, user)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 200, r.text
    assert r.json() == {"pkcs7": "cGtjczc="}


async def test_timestamp_route_threads_the_real_client_address_to_the_adapter(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _CapturingAdapter()
    monkeypatch.setattr(integrations_service, "get_eimzo_adapter", lambda: adapter)

    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()

    async with make_client(
        create_app(), lifespan=True, client_address=(CLIENT_IP, 51234)
    ) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 200, r.text
    assert adapter.captured_ip == CLIENT_IP


async def test_timestamp_route_surfaces_a_provider_outage_as_err_int_001_not_a_500(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrations_service, "get_eimzo_adapter", lambda: _OutageAdapter())

    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ERR-INT-001"


async def test_timestamp_route_carries_the_providers_own_reason_through(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4 / Task 7's whole reason to exist: a refusal must reach the
    caller with the provider's OWN machine-readable reason, not a bare 502 --
    `EimzoError.provider_status`/`.reason` were write-only until this route."""
    monkeypatch.setattr(integrations_service, "get_eimzo_adapter", lambda: _RefusingAdapter())

    user = await make_user(db)
    _, token, csrf = await make_session(db, user)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/eimzo/timestamp", json={"pkcs7": "cGtjczc="})
    assert r.status_code == 502
    body = r.json()["error"]
    assert body["code"] == "ERR-INT-002"
    assert body["details"] == {"provider_status": -11, "reason": "certificate_invalid"}


async def test_health_route_refuses_a_non_admin(db: AsyncSession) -> None:
    user = await make_user(db)  # executor_staff, no grants
    _, token, csrf = await make_session(db, user)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/eimzo/health")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_health_route_lets_sys_admin_see_the_combined_ping_and_info(
    db: AsyncSession,
) -> None:
    admin = await make_user(db, role_code="sys_admin")
    _, token, csrf = await make_session(db, admin)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/eimzo/health")
    assert r.status_code == 200, r.text
    # Default test settings run `eimzo_mode=mock` -- `MockEimzo.health()`'s own shape.
    assert r.json() == {"ping": "mock", "info": {"mode": "mock"}}


async def test_health_route_surfaces_a_provider_outage_as_err_int_001_not_a_500(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrations_service, "get_eimzo_adapter", lambda: _OutageAdapter())

    admin = await make_user(db, role_code="sys_admin")
    _, token, csrf = await make_session(db, admin)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/eimzo/health")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ERR-INT-001"
