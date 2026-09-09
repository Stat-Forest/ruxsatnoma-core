"""E-IMZO login: challenge lifecycle, cert expiry, legal-cert entry."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import DomainError
from app.core.security import hash_token
from app.main import create_app
from app.modules.auth import repo, service
from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    EimzoIdentity,
    RealEimzo,
    encode_mock_signed_challenge,
)
from app.modules.integrations.models import IntegrationLog
from tests.conftest import make_client

API = "/api/v1"


def unique_pinfl() -> str:
    return f"4{uuid.uuid4().int % 10**13:013d}"


async def get_challenge(client) -> str:
    r = await client.post(f"{API}/auth/eimzo/challenge")
    assert r.status_code == 200
    return r.json()["challenge"]


async def test_full_eimzo_login(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(challenge=challenge, pinfl=unique_pinfl(), full_name="ERI USER")
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
        assert r.status_code == 200
        assert "session" in r.cookies
        assert r.json()["role"]["code"] == "applicant"


async def test_challenge_single_use(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(challenge=challenge, pinfl=unique_pinfl(), full_name="U")
        signed = encode_mock_signed_challenge(identity)
        assert (
            await client.post(f"{API}/auth/eimzo/login", json={"signed_challenge": signed})
        ).status_code == 200
        r = await client.post(f"{API}/auth/eimzo/login", json={"signed_challenge": signed})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR-AUTH-004"


async def test_unknown_challenge_rejected(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        identity = EimzoIdentity(challenge="never-issued", pinfl=unique_pinfl(), full_name="U")
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 401


async def test_expired_cert_rejected(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="U",
            cert_expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "ERR-AUTH-004"
        # Finding 4 (final review): the challenge row is burned (used_at set) BEFORE
        # the expiry check runs, so a retry on the SAME challenge — even with a
        # valid, non-expired identity — must also fail. Pins that ordering.
        retry_identity = EimzoIdentity(challenge=challenge, pinfl=unique_pinfl(), full_name="U2")
        retry = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(retry_identity)},
        )
    assert retry.status_code == 401
    assert retry.json()["error"]["code"] == "ERR-AUTH-004"


async def test_garbage_signature_rejected(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/eimzo/login", json={"signed_challenge": "junk"})
    assert r.status_code == 401


async def test_legal_cert_still_logs_in_the_person(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="DIRECTOR",
            tin="555666777",
            legal_name="OOO DIR",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200
    assert r.json()["role"]["code"] == "applicant"


class _StubAdapter:
    """Answers `issue_challenge` with a fixed value regardless of the
    configured `eimzo_mode` — these tests force the branch through
    `issue_eimzo_challenge`'s own `mode` parameter instead."""

    def __init__(self, challenge: str) -> None:
        self._challenge = challenge

    async def issue_challenge(self, ip: str | None = None) -> str:
        return self._challenge


class _OutageAdapter:
    async def issue_challenge(self, ip: str | None = None) -> str:
        raise EimzoError("ERR-INT-001")


async def test_real_mode_takes_the_challenge_from_the_provider(db, monkeypatch) -> None:
    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _StubAdapter("PROVIDER-CHALLENGE"))
    challenge = await service.issue_eimzo_challenge(db, mode="real")
    assert challenge == "PROVIDER-CHALLENGE"
    # Nothing of ours was stored: their server owns the TTL and the matching.
    assert await repo.get_valid_otp(db, hash_token(challenge), purpose="eimzo_challenge") is None


async def test_mock_mode_still_mints_and_stores_our_own(db) -> None:
    challenge = await service.issue_eimzo_challenge(db, mode="mock")
    assert await repo.get_valid_otp(db, hash_token(challenge), purpose="eimzo_challenge")


async def test_a_provider_outage_while_issuing_a_challenge_is_an_integration_error(
    db, monkeypatch
) -> None:
    """Task 3's review found this exact class of defect on the signing
    routes (an `EimzoError` escaping as a bare 500): a provider outage while
    ISSUING a challenge must surface as `ERR-INT-001`, not a 500 and not an
    empty challenge silently handed to the browser."""
    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _OutageAdapter())
    with pytest.raises(DomainError) as exc:
        await service.issue_eimzo_challenge(db, mode="real")
    assert exc.value.code == "ERR-INT-001"
    assert exc.value.http_status == 503


# ---------------------------------------------------------------------------
# Task 5: one `integration_log` row per provider round trip. `RealEimzo`
# against `httpx.MockTransport` (never the real `e-imzo-server`, the same
# rule `test_eimzo_real.py` follows) so a genuine round trip is made and
# logged, both on success and on a refusal.
# ---------------------------------------------------------------------------

REAL_SETTINGS = Settings(
    eimzo_mode="real",
    eimzo_site_host="admin.ruxsatnoma-urmon.uz",
    _env_file=None,  # pyright: ignore[reportCallIssue]
)


async def _eimzo_log_tail(db: AsyncSession, count: int):
    """The last `count` eimzo rows. The test database is shared and nothing
    rolls a committed row back (backend/CLAUDE.md), so rows from earlier
    tests are always present; ids are uuid7 and therefore time-ordered
    (mirrors `test_oneid_login.py`'s own `_oneid_log_tail`)."""
    await db.flush()
    rows = (
        (
            await db.execute(
                select(IntegrationLog)
                .where(IntegrationLog.system == "eimzo")
                .order_by(IntegrationLog.id)
            )
        )
        .scalars()
        .all()
    )
    return rows[-count:]


async def test_a_refused_login_is_still_logged(db, monkeypatch) -> None:
    """`/backend/auth` answers a normal 200 with a non-1 status -- a refused
    login (`ERR-AUTH-004`), not a transport error -- and the round trip must
    still be logged."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": -10})

    monkeypatch.setattr(
        service,
        "get_eimzo_adapter",
        lambda: RealEimzo(REAL_SETTINGS, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(DomainError) as exc:
        await service.login_via_eimzo(
            db, signed_challenge="whatever", ip="91.0.0.7", user_agent="ua"
        )
    assert exc.value.code == "ERR-AUTH-004"

    (row,) = await _eimzo_log_tail(db, 1)
    assert row.endpoint == "/backend/auth"
    assert "whatever" not in str(row.meta)


async def test_a_provider_outage_during_login_is_also_logged(db, monkeypatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        service,
        "get_eimzo_adapter",
        lambda: RealEimzo(REAL_SETTINGS, transport=httpx.MockTransport(boom)),
    )
    with pytest.raises(DomainError) as exc:
        await service.login_via_eimzo(db, signed_challenge="x", ip=None, user_agent=None)
    assert exc.value.code == "ERR-INT-001"

    (row,) = await _eimzo_log_tail(db, 1)
    assert row.endpoint == "/backend/auth"
    assert row.http_status is None
