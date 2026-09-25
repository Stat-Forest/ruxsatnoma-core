"""E-IMZO login: challenge lifecycle, cert expiry, legal-cert entry."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config import Settings
from app.core.errors import DomainError
from app.core.security import hash_token
from app.db import make_session_factory
from app.main import create_app
from app.modules.auth import repo, service
from app.modules.auth.models import Applicant
from app.modules.integrations.adapters.eimzo import (
    EimzoError,
    EimzoIdentity,
    RealEimzo,
    encode_mock_signed_challenge,
)
from app.modules.integrations.models import IntegrationLog
from tests.conftest import make_client
from tests.modules.auth.test_otp import unique_phone
from tests.modules.auth.test_registration import (
    csrf_headers,
    registration_body,
    verified_phone_token,
)
from tests.modules.auth.test_sessions import make_user

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


async def test_an_inactive_legal_owner_is_refused(db):
    """M2 (final review): the legal branch's own `user.status != "active"`
    guard (`service.py:450-462`), untested until now — a suspended
    organisation account must not let a new key in through the back door."""
    stir = unique_stir()
    user, _row, _token, _csrf = await service.login_or_create_legal(
        db,
        stir=stir,
        org_name="OOO SUSPENDED",
        signer_pinfl=None,
        signer_name=None,
        ip=None,
        user_agent=None,
    )
    user.status = "blocked"
    await db.commit()

    with pytest.raises(DomainError) as exc:
        await service.login_or_create_legal(
            db,
            stir=stir,
            org_name="OOO SUSPENDED",
            signer_pinfl=unique_pinfl(),
            signer_name="ANOTHER EMPLOYEE",
            ip=None,
            user_agent=None,
        )
    assert exc.value.code == "ERR-AUTH-001"


async def test_the_audit_entry_records_the_signers_pinfl_and_name_not_a_second_account(db):
    """R3 (decision #226), untested until now: the person who acted is
    recorded in `audit.extra`, never as a separate account — `user.login`'s
    own entry carries `method: "eimzo_legal"` plus the signer's PINFL and
    name, and `user_id` is still the ORGANISATION's one account."""
    stir = unique_stir()
    signer_pinfl = unique_pinfl()
    user, _row, _token, _csrf = await service.login_or_create_legal(
        db,
        stir=stir,
        org_name="OOO AUDITED",
        signer_pinfl=signer_pinfl,
        signer_name="AUDITED DIRECTOR",
        ip=None,
        user_agent=None,
    )
    await db.commit()

    from app.modules.audit.models import AuditLog

    entry = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "user.login", AuditLog.user_id == user.id)
                .order_by(AuditLog.occurred_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.extra == {
        "method": "eimzo_legal",
        "signer_pinfl": signer_pinfl,
        "signer_name": "AUDITED DIRECTOR",
    }


async def test_adopting_an_unverified_pre_existing_applicant_heals_its_name_and_verification(db):
    """M1 (final review): a row from the retired `attach_legal` could carry
    ANY name under a `basis=poa` attach with no verification of its own — the
    organisation's own certificate is stronger proof than that ever was, so
    adopting a row nobody had verified corrects the name to the certificate's
    own `O` field and stamps `verified_at`/`verify_source`."""
    stir = unique_stir()
    existing = Applicant(kind="legal", stir=stir, name="POSSIBLY SQUATTED NAME")
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
            legal_name="OOO REAL NAME",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["applicant"]["name"] == "OOO REAL NAME"
    assert r.json()["applicant"]["verified_at"] is not None


# --- C2 (amendment to R2, decision #226, 2026-09-26): staff keep their staff
# account when their key happens to be issued under an organisation TIN ------


async def test_staff_with_an_org_certificate_logs_into_their_own_staff_account(db):
    """A real organisation key names the employee's own PINFL alongside the
    org TIN. Before this amendment, ANY certificate with a TIN skipped
    `login_or_create_by_pinfl` entirely, so a leshoz inspector signing in
    with the leshoz's own key landed in a freshly-created applicant cabinet
    for "leshoz X" instead of their working account — invisible, since
    nothing errors (final review C2)."""
    pinfl = unique_pinfl()
    staff = await make_user(db, role_code="executor_staff", pinfl=pinfl)
    await db.commit()

    app = create_app()
    stir = unique_stir()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=pinfl,
            full_name="INSPECTOR",
            tin=stir,
            legal_name="LESHOZ X",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == str(staff.id)
    assert r.json()["role"]["code"] == "executor_staff"
    # No applicant cabinet for "LESHOZ X" was ever created for this login.
    applicant = await service.get_own_applicant(db, staff.id)
    assert applicant is None


async def test_an_unknown_pinfl_with_an_org_certificate_still_opens_the_org_cabinet(db):
    """The other half of C2: a PINFL matching NO existing user is not staff,
    so the TIN still opens the organisation's own cabinet — unchanged from
    R2 as first shipped."""
    app = create_app()
    stir = unique_stir()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="DIRECTOR",
            tin=stir,
            legal_name="OOO NEW",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["applicant"]["kind"] == "legal"
    assert r.json()["applicant"]["stir"] == stir


async def test_an_applicant_owning_the_pinfl_does_not_divert_the_org_key_to_them(db):
    """An `applicant`-role match is not STAFF (C2's own wording), so it falls
    through the same way an unknown PINFL does — a citizen who also holds a
    personal cabinet under this exact PINFL must not have someone else's
    organisation key silently log them into their own personal account
    instead of opening the organisation's cabinet."""
    pinfl = unique_pinfl()
    individual = await make_user(db, role_code="applicant", pinfl=pinfl)
    await db.commit()

    app = create_app()
    stir = unique_stir()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=pinfl,
            full_name="SAME PERSON",
            tin=stir,
            legal_name="OOO SAME",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] != str(individual.id)
    assert r.json()["applicant"]["kind"] == "legal"
    assert r.json()["applicant"]["stir"] == stir


# --- I1/R6 (final review): a legal cabinet completes the same registration -
# as an individual before its own SMS/notifications become reachable -------


async def test_a_legal_cabinet_completes_the_same_registration_flow_as_an_individual(db):
    """`login_or_create_legal` creates the `Applicant` row at LOGIN (it needs
    the row to exist for the STIR lookup itself), so `registration_complete`
    cannot mean "an applicant exists" for a legal cabinet the way it does
    for an individual (whose row is created ONLY by `complete_registration`)
    — it must still gate on the same phone-by-OTP-plus-consents flow, or
    `notifications.service._recipient_reachable` silently drops every SMS to
    the organisation (final review I1)."""
    stir = unique_stir()
    phone = unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        challenge = await get_challenge(client)
        identity = EimzoIdentity(
            challenge=challenge,
            pinfl=unique_pinfl(),
            full_name="DIRECTOR",
            tin=stir,
            legal_name="OOO REG",
        )
        r = await client.post(
            f"{API}/auth/eimzo/login",
            json={"signed_challenge": encode_mock_signed_challenge(identity)},
        )
        assert r.status_code == 200, r.text
        assert r.json()["applicant"]["kind"] == "legal"
        me = await client.get(f"{API}/auth/me")
        assert me.json()["registration_complete"] is False

        token = await verified_phone_token(client, phone, db=db)
        complete = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(phone, token),
            headers=csrf_headers(client),
        )
        assert complete.status_code == 200, complete.text
        body = complete.json()
        assert body["registration_complete"] is True
        assert body["applicant"]["kind"] == "legal"
        assert body["applicant"]["stir"] == stir
        assert body["applicant"]["phone"] == phone

        me2 = await client.get(f"{API}/auth/me")
        assert me2.json()["registration_complete"] is True

        # Symmetric with the individual flow: a second attempt is refused,
        # not a second set of consents / a rewritten phone.
        again = await verified_phone_token(client, unique_phone(), db=db)
        refused = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(phone, again),
            headers=csrf_headers(client),
        )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "ERR-AUTH-012"


# --- I2 (final review): concurrent first logins for one STIR ----------------


async def test_two_concurrent_first_logins_for_a_fresh_stir_do_not_500(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """`login_or_create_legal`'s lookup-then-insert was not atomic: two
    employees presenting a BRAND NEW STIR at the same moment both saw no
    `applicants` row and both inserted one, so the second commit hit
    `applicants.stir`'s own UNIQUE constraint as a bare `IntegrityError` —
    an unhandled 500 (final review I2). Two REAL, independent sessions
    (`test_public_surface.py`'s own `make_session_factory(engine)` pattern),
    since a single session cannot demonstrate a lock against itself."""
    stir = unique_stir()
    factory = make_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        user_a, _row_a, _token_a, _csrf_a = await service.login_or_create_legal(
            session_a,
            stir=stir,
            org_name="OOO RACE",
            signer_pinfl=None,
            signer_name=None,
            ip=None,
            user_agent=None,
        )
        # session_a holds the advisory lock until it commits — session_b's own
        # call must block on it rather than racing the same fresh-STIR insert.
        task_b = asyncio.create_task(
            service.login_or_create_legal(
                session_b,
                stir=stir,
                org_name="OOO RACE",
                signer_pinfl=None,
                signer_name=None,
                ip=None,
                user_agent=None,
            )
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "session_b should still be blocked on session_a's advisory lock"

        await session_a.commit()  # releases the advisory lock

        user_b, _row_b, _token_b, _csrf_b = await asyncio.wait_for(task_b, timeout=5)
        await session_b.commit()

        assert user_b.id == user_a.id
    finally:
        await session_a.close()
        await session_b.close()


async def test_two_concurrent_first_logins_for_a_preexisting_unowned_applicant_link_only_once(
    engine: AsyncEngine, db: AsyncSession
) -> None:
    """The R1 adopt path's own race (final review I2): both requests saw
    `owner_user_id IS NULL` and both `UPDATE`d it, so the second silently
    overwrote the first — the first user kept a live session but owned no
    applicant, and `complete_registration`'s `assert user.pinfl is not None`
    (unreachable for that account, R1) turned its own attempt to register
    into a second 500. The advisory lock serializes the two adopt attempts
    into one winner instead."""
    stir = unique_stir()
    existing = Applicant(kind="legal", stir=stir, name="OOO PRE-EXISTING")
    db.add(existing)
    await db.flush()
    await db.commit()
    existing_id = existing.id

    factory = make_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        user_a, _row_a, _token_a, _csrf_a = await service.login_or_create_legal(
            session_a,
            stir=stir,
            org_name="OOO PRE-EXISTING",
            signer_pinfl=None,
            signer_name=None,
            ip=None,
            user_agent=None,
        )
        task_b = asyncio.create_task(
            service.login_or_create_legal(
                session_b,
                stir=stir,
                org_name="OOO PRE-EXISTING",
                signer_pinfl=None,
                signer_name=None,
                ip=None,
                user_agent=None,
            )
        )
        await asyncio.sleep(0.3)
        assert not task_b.done(), "session_b should still be blocked on session_a's advisory lock"

        await session_a.commit()

        user_b, _row_b, _token_b, _csrf_b = await asyncio.wait_for(task_b, timeout=5)
        await session_b.commit()

        assert user_b.id == user_a.id  # linked once, never a second orphaned user
    finally:
        await session_a.close()
        await session_b.close()

    async with factory() as fresh:
        refreshed = await fresh.get(Applicant, existing_id)
        assert refreshed is not None
        assert refreshed.owner_user_id == user_a.id
