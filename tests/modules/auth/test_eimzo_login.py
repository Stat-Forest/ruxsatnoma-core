"""E-IMZO login: challenge lifecycle, cert expiry, legal-cert entry."""

import uuid
from datetime import UTC, datetime, timedelta

from app.main import create_app
from app.modules.auth.adapters.eimzo import EimzoIdentity, encode_mock_signed_challenge
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
