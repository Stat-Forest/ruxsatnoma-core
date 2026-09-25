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


def unique_stir() -> str:
    return f"9{uuid.uuid4().int % 10**8:08d}"


async def test_legal_cert_logs_into_the_organisations_own_cabinet(db):
    """R2/R1 (decision #226): `identity.tin` present logs in the ORGANISATION
    by STIR — its own account, not the signer's personal one — whatever
    PINFL the certificate also carries."""
    app = create_app()
    stir = unique_stir()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="DIRECTOR",
            tin=stir,
            legal_name="OOO DIR",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"]["code"] == "applicant"
    assert body["user"]["pinfl"] is None
    assert body["applicant"]["kind"] == "legal"
    assert body["applicant"]["stir"] == stir
    assert body["applicant"]["name"] == "OOO DIR"


async def test_a_second_employee_of_the_same_organisation_lands_in_the_same_cabinet(db):
    """One cabinet per STIR (decision #226): a DIFFERENT signer's PINFL, the
    SAME org STIR, must resolve to the identical `applicants`/`users` row."""
    app = create_app()
    stir = unique_stir()
    async with make_client(app, lifespan=True) as client:
        first_challenge = await get_challenge(client)
        first = EimzoIdentity(
            challenge=first_challenge,
            pinfl=unique_pinfl(),
            full_name="FIRST DIRECTOR",
            tin=stir,
            legal_name="OOO SHARED",
        )
        r1 = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(first)},
        )
        assert r1.status_code == 200, r1.text
        first_applicant_id = r1.json()["applicant"]["id"]
        first_user_id = r1.json()["user"]["id"]

        second_challenge = await get_challenge(client)
        second = EimzoIdentity(
            challenge=second_challenge,
            pinfl=unique_pinfl(),
            full_name="SECOND EMPLOYEE",
            tin=stir,
            legal_name="OOO SHARED",
        )
        r2 = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(second)},
        )
    assert r2.status_code == 200, r2.text
    assert r2.json()["applicant"]["id"] == first_applicant_id
    assert r2.json()["user"]["id"] == first_user_id


async def test_a_pre_existing_unowned_legal_applicant_is_linked_on_first_login(db):
    """R1: an `applicants` row created earlier by the retired `attach_legal`
    (`owner_user_id IS NULL`) gets its account on the organisation's first
    login under this stage — no data migration."""
    from app.modules.auth.models import Applicant

    stir = unique_stir()
    existing = Applicant(kind="legal", stir=stir, name="OOO PRE-EXISTING")
    db.add(existing)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="DIRECTOR",
            tin=stir,
            legal_name="OOO PRE-EXISTING",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["applicant"]["id"] == str(existing.id)


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


async def test_a_non_200_login_refusal_carries_the_providers_reason(db, monkeypatch) -> None:
    """Minor 9 (final review): `login_via_eimzo` used to `raise
    err(exc.err_code)` with no `details`, discarding `EimzoError.
    provider_status`/`.reason` -- present here since `/backend/auth`
    answered a non-200 with a JSON body `_send` already parses and attaches
    to the exception (mirrors `test_eimzo_real.py::
    test_a_non_200_response_with_a_status_field_carries_it_on_the_exception`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"status": -11, "message": "bad cert"})

    monkeypatch.setattr(
        service,
        "get_eimzo_adapter",
        lambda: RealEimzo(REAL_SETTINGS, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(DomainError) as exc:
        await service.login_via_eimzo(db, signed_challenge="x", ip=None, user_agent=None)
    assert exc.value.code == "ERR-INT-002"
    assert exc.value.details == {"provider_status": -11, "reason": "certificate_invalid"}


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


async def test_a_legal_entity_certificate_logs_in_by_tin_over_real_mode(db, monkeypatch) -> None:
    """R2 (decision #226): a certificate carrying an org TIN but no personal
    PINFL still logs in -- as the ORGANISATION, by STIR, over the real-mode
    wire (`eimzo_wire.read_subject`'s own OID map, `subjectName`'s
    `1.2.860.3.16.1.1`). Before stage 18 this same shape used to be refused
    at the personal-login PINFL check (finding 6, final review) because the
    legal branch did not exist yet; now it is the legal branch's own case."""
    sample = {
        "subjectCertificateInfo": {
            "serialNumber": "org-cert-2",
            "X500Name": "CN=BURCHMULLA LESHOZ",
            "subjectName": {
                "1.2.860.3.16.1.1": "301234567",  # org STIR only -- no personal PINFL
                "CN": "BURCHMULLA LESHOZ",
            },
            "validFrom": "2026-05-25 15:47:22",
            "validTo": "2099-06-24 15:47:22",
        },
        "status": 1,
        "message": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample)

    monkeypatch.setattr(
        service,
        "get_eimzo_adapter",
        lambda: RealEimzo(REAL_SETTINGS, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(service, "get_settings", lambda: REAL_SETTINGS)
    user, _row, _token, _csrf = await service.login_via_eimzo(
        db, signed_challenge="x", ip=None, user_agent=None
    )
    assert user.pinfl is None
    applicant = await service.get_own_applicant(db, user.id)
    assert applicant is not None
    assert applicant.kind == "legal"
    assert applicant.stir == "301234567"


async def test_a_certificate_with_neither_pinfl_nor_tin_refuses_login_cleanly(
    db, monkeypatch
) -> None:
    """R2's own last sentence: a certificate with neither is refused as
    before this stage (`ERR-AUTH-004`), not an uncaught `IntegrityError`/500."""
    sample = {
        "subjectCertificateInfo": {
            "serialNumber": "org-cert-3",
            "X500Name": "CN=NOBODY",
            "subjectName": {"CN": "NOBODY"},
            "validFrom": "2026-05-25 15:47:22",
            "validTo": "2099-06-24 15:47:22",
        },
        "status": 1,
        "message": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample)

    monkeypatch.setattr(
        service,
        "get_eimzo_adapter",
        lambda: RealEimzo(REAL_SETTINGS, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(service, "get_settings", lambda: REAL_SETTINGS)
    with pytest.raises(DomainError) as exc:
        await service.login_via_eimzo(db, signed_challenge="x", ip=None, user_agent=None)
    assert exc.value.code == "ERR-AUTH-004"
    assert exc.value.http_status == 401
