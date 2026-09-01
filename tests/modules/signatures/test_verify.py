from datetime import UTC, datetime, timedelta

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
        signed_at=NOW,
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
