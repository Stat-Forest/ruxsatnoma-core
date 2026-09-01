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
