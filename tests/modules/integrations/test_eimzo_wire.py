"""Wire-format parsing: dates, subjects and the four extra pkcs7-verify
status codes (stage 5.2 plan, Task 2). Pure functions, no adapter, no I/O —
the vendor's own response samples are enough to exercise all of it."""

import copy
from datetime import UTC, datetime

from app.modules.integrations.adapters.eimzo import EIMZO_STATUS_REASONS
from app.modules.integrations.adapters.eimzo_wire import (
    parse_provider_datetime,
    read_subject,
    verification_from_pkcs7_info,
)
from tests.modules.integrations.eimzo_samples import VENDOR_ATTACHED_SAMPLE


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


def test_verification_never_carries_the_signers_pinfl_or_the_ocsp_response() -> None:
    """Finding 5 (final review): `raw` used to be `pkcs7Info` copied
    wholesale minus only `documentBase64`, so `certificate[0].subjectInfo`'s
    PINFL, `subjectName`'s `UID=<pinfl>`, the OCSP response and the raw
    public key all rode along into an append-only column served whole to
    every co-signer (`GET /api/v1/signatures`) — a citizen who signed as one
    party could read every OTHER signer's own PINFL. Replaced with an
    explicit allow-list of what a verdict actually rests on."""
    result = verification_from_pkcs7_info(VENDOR_ATTACHED_SAMPLE)
    blob = str(result.raw)
    assert "31234567890123" not in blob  # the signer's own PINFL
    assert "subjectInfo" not in blob
    assert "subjectName" not in blob
    assert "OCSPResponse" not in blob
    assert "publicKey" not in blob
    signer_evidence = result.raw["signers"][0]
    # What a verdict genuinely rests on is still there.
    assert signer_evidence["verified"] is True
    assert signer_evidence["certificateVerified"] is True
    assert signer_evidence["certificateValidAtSigningTime"] is True
    assert signer_evidence["certificateSerialNumber"] == "218712ed3"
    assert signer_evidence["certificateValidFrom"] == "2026-05-25 15:47:22"
    assert signer_evidence["certificateValidTo"] == "2026-06-24 15:47:22"


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


# ---------------------------------------------------------------------------
# Finding 4 (final review): a malformed provider payload is a VERDICT, not an
# exception -- `verification_from_pkcs7_info`'s own docstring already claims
# it never raises, but a certificate entry missing `serialNumber`/`validFrom`/
# `validTo`, or one whose date string `fromisoformat` rejects, used to escape
# as KeyError/ValueError straight through `sign()`'s `except EimzoError` as an
# unhandled 500 -- no signature row, no audit entry, no integration-log row.
# ---------------------------------------------------------------------------


def test_a_certificate_entry_missing_a_required_field_does_not_raise() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    del sample["pkcs7Info"]["signers"][0]["certificate"][0]["validFrom"]
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == 1
    assert result.subject_certificate is None


def test_a_certificate_entry_with_an_unparseable_date_does_not_raise() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["certificate"][0]["validFrom"] = "not-a-date"
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == 1
    assert result.subject_certificate is None


def test_an_unparseable_signing_time_does_not_raise() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["signingTime"] = "not-a-date"
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == 1
    assert result.signed_at is None


def test_an_unparseable_timestamp_time_does_not_raise() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["timeStampInfo"]["time"] = "not-a-date"
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == 1
    assert result.timestamp_token is None


# ---------------------------------------------------------------------------
# Ruling FR-1 (final review): the vendor's OUTER `status` may mean only
# "request processed", not "signature good" -- `pkcs7Info.signers[0]` itself
# carries three independent verification booleans, all present in the
# vendor's own sample, that `status: 1` alone does not guarantee.
# ---------------------------------------------------------------------------


def test_an_explicit_false_verified_boolean_refuses_despite_status_1() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["verified"] = False
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == -10
    assert EIMZO_STATUS_REASONS[result.status_code] == "signature_invalid"


def test_an_explicit_false_certificate_verified_boolean_refuses_despite_status_1() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["certificateVerified"] = False
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == -11
    assert EIMZO_STATUS_REASONS[result.status_code] == "certificate_invalid"


def test_an_explicit_false_certificate_valid_at_signing_time_boolean_refuses() -> None:
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    sample["pkcs7Info"]["signers"][0]["certificateValidAtSigningTime"] = False
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == -12
    assert EIMZO_STATUS_REASONS[result.status_code] == "certificate_invalid_at_signing"


def test_an_absent_verification_boolean_keeps_todays_behaviour() -> None:
    # Only an EXPLICIT `False` refuses -- a provider that never sends one of
    # these fields at all must not have every signature it approves flip to
    # invalid.
    sample = copy.deepcopy(VENDOR_ATTACHED_SAMPLE)
    del sample["pkcs7Info"]["signers"][0]["certificateValidAtSigningTime"]
    result = verification_from_pkcs7_info(sample)
    assert result.status_code == 1
