import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.signatures.verify import build_verdict

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _result(status_code: int = 1, **kw):
    from app.modules.integrations.adapters.eimzo import EimzoCertificateInfo, EimzoVerification

    cert = EimzoCertificateInfo(
        serial_number="SER-1",
        issuer="ISS-1",
        subject="CN=A",
        pinfl_or_stir="12345678901",
        valid_from=NOW - timedelta(days=30),
        valid_to=NOW + timedelta(days=300),
    )
    return EimzoVerification(
        status_code=status_code,
        subject_certificate=kw.get("cert", cert),
        signed_at=kw.get("signed_at", NOW),
        timestamp_token="TS",
        raw={},
    )


def test_a_good_signature_with_an_active_certificate_is_valid():
    verdict = build_verdict(_result(), cert_status="active", now=NOW)
    assert verdict.status == "valid"
    assert verdict.reason is None


def test_a_revoked_certificate_invalidates_an_otherwise_good_signature():
    verdict = build_verdict(_result(), cert_status="revoked", now=NOW)
    assert verdict.status == "invalid"
    assert verdict.reason == "certificate_revoked"


def test_each_eimzo_status_code_keeps_its_own_reason():
    for code, reason in {
        -1: "certificate_status_unknown",
        -5: "clock_skew",
        -10: "signature_invalid",
        -11: "certificate_invalid",
        -12: "certificate_invalid_at_signing",
        -20: "challenge_expired",
    }.items():
        verdict = build_verdict(_result(code), cert_status="active", now=NOW)
        assert verdict.status == "invalid"
        assert verdict.reason == reason, code


def test_the_record_carries_the_whole_chain_for_later_reading():
    verdict = build_verdict(_result(), cert_status="active", now=NOW)
    assert verdict.record["status_code"] == 1
    assert verdict.record["certificate_status"] == "active"
    assert verdict.record["timestamp_token"] == "TS"
    assert verdict.record["checked_at"] == NOW.isoformat()


def test_the_record_carries_the_certificates_own_identity_fields():
    verdict = build_verdict(_result(), cert_status="active", now=NOW)
    assert verdict.record["certificate_serial_number"] == "SER-1"
    assert verdict.record["certificate_issuer"] == "ISS-1"
    assert verdict.record["certificate_subject"] == "CN=A"
    assert verdict.record["certificate_valid_from"] == (NOW - timedelta(days=30)).isoformat()
    assert verdict.record["certificate_valid_to"] == (NOW + timedelta(days=300)).isoformat()


def test_the_record_marks_a_missing_certificate_explicitly_rather_than_omitting_the_keys():
    from app.modules.integrations.adapters.eimzo import EimzoVerification

    # The shape a real verification takes when it fails before a certificate
    # could even be parsed (mirrors `_unparseable_signature()` in eimzo.py).
    result = EimzoVerification(
        status_code=-10, subject_certificate=None, signed_at=None, timestamp_token=None, raw={}
    )
    verdict = build_verdict(result, cert_status="active", now=NOW)
    assert verdict.record["certificate_serial_number"] is None
    assert verdict.record["certificate_issuer"] is None
    assert verdict.record["certificate_subject"] is None
    assert verdict.record["certificate_valid_from"] is None
    assert verdict.record["certificate_valid_to"] is None


def test_a_missing_certificate_fails_closed_instead_of_passing_as_valid():
    # status_code == 1 and an active cert_status must not, by themselves, be
    # enough to call a signature valid — nothing enforces that the adapter
    # actually examined a certificate (finding 1, fix round 2).
    verdict = build_verdict(_result(cert=None), cert_status="active", now=NOW)
    assert verdict.status == "invalid"
    assert verdict.reason == "certificate_missing"


def test_the_validity_window_is_checked_against_signed_at_not_now():
    # signed_at falls outside the certificate's window; `now` sits
    # comfortably inside it. If the code compared the window against `now`
    # instead of `signed_at`, this would come back valid — the wrong answer.
    result = _result(signed_at=NOW - timedelta(days=400))
    verdict = build_verdict(result, cert_status="active", now=NOW)
    assert verdict.status == "invalid"
    assert verdict.reason == "certificate_invalid_at_signing"


def test_a_certificate_that_expires_after_signing_does_not_invalidate_the_signature():
    # Mirror of the test above (ruling 5): signed_at is inside the window,
    # `now` is 400 days later — past valid_to. If the code compared the
    # window against `now`, this would come back invalid. A permit signed
    # while the certificate was good stays valid after the certificate
    # expires.
    result = _result(signed_at=NOW)
    verdict = build_verdict(result, cert_status="active", now=NOW + timedelta(days=400))
    assert verdict.status == "valid"
    assert verdict.reason is None


# ---------------------------------------------------------------------------
# Task 6: `reverify()` names the check it actually made — the async,
# service-level counterpart of the pure `build_verdict` tests above. A
# `RealEimzo`-shaped stub with `revocation_checkable = False` stands in for
# the live adapter, the same way `test_provider_outage.py`'s `_OutageAdapter`
# and `test_ri05.py`'s `_FakeAdapter` stand in for it elsewhere in this
# module — no network, no e-imzo-server.
# ---------------------------------------------------------------------------


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call — `users.pinfl` is
    UNIQUE and this test database is shared and persistent (mirrors
    `test_sign.py`'s own helper)."""
    return f"{secrets.randbelow(10**14):014d}"


class _DateOnlyAdapter:
    """Stands in for `RealEimzo.certificate_status`: no revocation check is
    possible, only the validity window against `valid_to` — Task 6's own
    scenario (plan 05.2 R3)."""

    revocation_checkable = False

    async def certificate_status(self, *, serial: str, issuer: str, valid_to: datetime) -> str:
        return "expired" if valid_to < datetime.now(UTC) else "active"


@pytest.mark.asyncio
async def test_reverify_in_real_mode_names_what_it_could_check(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.integrations.adapters.eimzo import encode_mock_signature
    from app.modules.signatures import service
    from tests.modules.auth.test_sessions import make_user

    document = b"the-permit-bytes"
    user = await make_user(db, pinfl=_pinfl())
    assert user.pinfl is not None  # narrows User.pinfl's nullable column type
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SER-{uuid.uuid4().hex[:10]}", issuer="ISS-1", pinfl=user.pinfl
    )
    signature = await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=document,
        pkcs7=pkcs7,
        user=user,
    )
    await db.commit()

    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _DateOnlyAdapter())
    record = (await service.reverify(db, signature_id=signature.id, user=user)).verification

    assert record["rechecked"] == "certificate_validity_only"
    assert record["revocation_checked"] is False


# ---------------------------------------------------------------------------
# Task 6's own review finding: `_reconcile_status` must never let a
# date-only answer (`revocation_checkable=False`) un-revoke a certificate
# already marked `"revoked"` — confirm-or-downgrade, never upgrade
# (`reverify()`'s own docstring). Decided here: a certificate already
# revoked stays revoked against such an adapter's answer, in both the
# `certificates` row itself and in what a `reverify()` call REPORTS about a
# signature bound to it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_status_never_lets_a_date_only_answer_un_revoke_a_certificate(
    db: AsyncSession,
) -> None:
    from app.db import uuid7
    from app.modules.signatures import service
    from app.modules.signatures.models import Certificate

    now = datetime.now(UTC)
    cert = Certificate(
        id=uuid7(),
        user_id=None,
        serial_number=f"SER-{uuid.uuid4().hex[:12]}",
        issuer=f"ISS-{uuid.uuid4().hex[:8]}",
        subject="CN=Test Signer",
        pinfl_or_stir="12345678901",
        valid_from=now - timedelta(days=30),
        valid_to=now + timedelta(days=300),  # not yet expired by date
        status="revoked",
        revoked_at=now - timedelta(days=1),
    )
    db.add(cert)
    await db.flush()

    # A date-only adapter's "active" answer (RealEimzo's own shape: the cert
    # has not yet reached valid_to) must not overwrite a KNOWN revocation.
    await service._reconcile_status(db, cert, "active", revocation_checkable=False)

    assert cert.status == "revoked"
    assert cert.revoked_at is not None


@pytest.mark.asyncio
async def test_reverify_never_reports_a_revoked_certificates_signature_valid_in_real_mode(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integration this module's whole discipline depends on: even once
    `_reconcile_status` refuses to un-revoke the CERTIFICATE row, `reverify()`
    itself must read that guarded value back — not the adapter's raw,
    unreconciled answer — or the SIGNATURE record it writes would still
    claim "valid" for a certificate everyone else can see is revoked."""
    from app.modules.integrations.adapters.eimzo import encode_mock_signature
    from app.modules.signatures import service
    from tests.modules.auth.test_sessions import make_user

    document = b"the-permit-bytes"
    user = await make_user(db, pinfl=_pinfl())
    assert user.pinfl is not None  # narrows User.pinfl's nullable column type
    pkcs7 = encode_mock_signature(
        document=document, serial=f"SER-{uuid.uuid4().hex[:10]}", issuer="ISS-1", pinfl=user.pinfl
    )
    signature = await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=document,
        pkcs7=pkcs7,
        user=user,
    )
    await db.commit()

    cert = await service.get_certificate(db, signature.certificate_id)
    cert.status = "revoked"
    cert.revoked_at = datetime.now(UTC)
    await db.commit()

    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _DateOnlyAdapter())
    new_row = await service.reverify(db, signature_id=signature.id, user=user)

    assert new_row.verification_status == "invalid"
    assert new_row.verification["reason"] == "certificate_revoked"
    assert new_row.verification["certificate_status"] == "revoked"
    await db.refresh(cert)
    assert cert.status == "revoked"
    assert cert.revoked_at is not None
