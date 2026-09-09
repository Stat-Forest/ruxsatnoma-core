"""Wire-format parsing: dates, subjects and the four extra pkcs7-verify
status codes (stage 5.2 plan, Task 2). Pure functions, no adapter, no I/O —
the vendor's own response samples are enough to exercise all of it."""

from datetime import UTC, datetime

from app.modules.integrations.adapters.eimzo import EIMZO_STATUS_REASONS
from app.modules.integrations.adapters.eimzo_wire import (
    certificate_from_subject_info,
    parse_provider_datetime,
    read_subject,
    verification_from_pkcs7_info,
)
from tests.modules.integrations.eimzo_samples import VENDOR_ATTACHED_SAMPLE, VENDOR_AUTH_SAMPLE


def test_provider_datetime_is_parsed_and_made_aware() -> None:
    # The vendor's own sample, published 2026-05-25: a space, no timezone.
    parsed = parse_provider_datetime("2026-05-25 15:47:22")
    assert parsed == datetime(2026, 5, 25, 15, 47, 22, tzinfo=UTC)
    assert parsed.tzinfo is not None


def test_provider_datetime_accepts_the_iso_form_too() -> None:
    assert parse_provider_datetime("2026-05-25T15:47:22Z") == datetime(
        2026, 5, 25, 15, 47, 22, tzinfo=UTC
    )


def test_subject_prefers_the_personal_pinfl_over_the_org_tin() -> None:
    # 1.2.860.3.16.1.2 is the person's PINFL, 1.2.860.3.16.1.1 the org's STIR.
    # `signatures._ownership_reason` compares against the SIGNER, so the
    # personal identifier is the one that must land in `pinfl_or_stir`.
    subject, identifier, legal_tin = read_subject(
        {
            "CN": "ALIYEV ALI ALIYEVICH",
            "1.2.860.3.16.1.2": "31234567890123",
            "1.2.860.3.16.1.1": "301234567",
            "O": "BURCHMULLA LESHOZ",
        }
    )
    assert identifier == "31234567890123"
    assert legal_tin == "301234567"
    assert "ALIYEV ALI ALIYEVICH" in subject


def test_subject_falls_back_to_the_org_stir_with_no_personal_pinfl() -> None:
    # A legal-entity-only certificate: no personal PINFL at all. The org STIR
    # is what `pinfl_or_stir` must carry then — `read_subject`'s own
    # docstring: "a legal-entity certificate still names the human who holds
    # it", but the identifier still has to resolve to *something*.
    subject, identifier, legal_tin = read_subject(
        {"CN": "BURCHMULLA LESHOZ DIRECTOR", "1.2.860.3.16.1.1": "301234567"}
    )
    assert identifier == "301234567"
    assert legal_tin == "301234567"
    assert subject == "BURCHMULLA LESHOZ DIRECTOR"


def test_timestamp_status_codes_have_their_own_reasons() -> None:
    assert EIMZO_STATUS_REASONS[-21] == "timestamp_signature_invalid"
    assert EIMZO_STATUS_REASONS[-22] == "timestamp_certificate_invalid"
    assert EIMZO_STATUS_REASONS[-23] == "timestamp_certificate_invalid_at_signing"
    assert EIMZO_STATUS_REASONS[0] == "provider_bad_response"


def test_verification_never_carries_the_document_bytes() -> None:
    # `/backend/pkcs7/verify/attached` returns `documentBase64`. The
    # signatures module stores `raw` verbatim in an append-only column and
    # serves it to every co-signer — the document must not travel in it.
    result = verification_from_pkcs7_info(VENDOR_ATTACHED_SAMPLE)
    assert "documentBase64" not in result.raw
    assert result.raw["signers"][0]["paramSetOID"] == "1.2.860.3.15.2.1.2.1.1"


def test_verification_reads_the_signer_certificate_and_signing_time() -> None:
    result = verification_from_pkcs7_info(VENDOR_ATTACHED_SAMPLE)
    assert result.status_code == 1
    assert result.subject_certificate is not None
    assert result.subject_certificate.serial_number == "218712ed3"
    assert result.subject_certificate.pinfl_or_stir == "31234567890123"
    assert result.subject_certificate.valid_from == datetime(2026, 5, 25, 15, 47, 22, tzinfo=UTC)
    assert result.signed_at == datetime(2026, 5, 25, 15, 48, 10, tzinfo=UTC)
    # The trusted timestamp's own time, normalized through the same helper —
    # never the naive string the provider sent.
    assert result.timestamp_token == "2026-05-25T15:48:12+00:00"


def test_verification_of_an_empty_response_does_not_raise() -> None:
    # A totally failed verification may arrive with no `pkcs7Info` at all
    # (`Pkcs7VerifyJsonResponse`'s other constructor takes a bare
    # `failedSignerInfo`) — this module reports a verdict, it does not raise.
    result = verification_from_pkcs7_info({"status": -10, "message": "bad pkcs7"})
    assert result.status_code == -10
    assert result.subject_certificate is None
    assert result.signed_at is None
    assert result.raw == {}


def test_certificate_from_subject_info_reads_the_vendor_auth_sample() -> None:
    # `certificate_from_subject_info` has no test of its own in the brief;
    # added because Task 3 depends on it and it is cheap to pin now against
    # the vendor's own sample rather than leaving it unexercised until then.
    #
    # `pinfl_or_stir` is asserted here on purpose: `VENDOR_AUTH_SAMPLE`'s
    # `subjectName` must be OID-keyed (controller ruling T2-1) for this
    # fixture to exercise the actual extraction Task 3's ERI-login path
    # depends on — an abbreviated `{"UID": ..., "CN": ...}` shape would
    # parse without error and silently yield an empty identifier, which is
    # exactly the gap that stayed invisible before this assertion existed.
    cert = certificate_from_subject_info(VENDOR_AUTH_SAMPLE["subjectCertificateInfo"])
    assert cert.serial_number == "218712ed3"
    assert cert.issuer == "CN=TESTOV TEST TESTOVICH"  # X500Name, the only issuer-shaped field here
    assert cert.pinfl_or_stir == "12345678901234"
    assert cert.valid_from == datetime(2026, 5, 25, 15, 47, 22, tzinfo=UTC)
    assert cert.valid_to == datetime(2026, 6, 24, 15, 47, 22, tzinfo=UTC)
