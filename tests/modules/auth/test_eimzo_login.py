"""E-IMZO login: challenge lifecycle, cert expiry, legal-cert entry."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import DomainError
from app.core.security import hash_token
from app.main import create_app
from app.modules.auth import repo, service
from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    EimzoIdentity,
    encode_mock_signed_challenge,
)
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
