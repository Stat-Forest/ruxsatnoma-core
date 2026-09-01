"""E-IMZO adapter seam extension (plan 03.8 Task 2): document verification and
the seven design/04 §2.5 status codes, on top of the unchanged login mock."""

import pytest

from app.modules.integrations.adapters.eimzo import (
    EIMZO_STATUS_REASONS,
    encode_mock_signature,
    get_eimzo_adapter,
)


@pytest.mark.asyncio
async def test_verify_detached_returns_the_certificate_of_the_signer():
    adapter = get_eimzo_adapter()
    pkcs7 = encode_mock_signature(
        document=b"the-document", serial="SER-1", issuer="ISS-1", pinfl="12345678901"
    )
    result = await adapter.verify_detached(document=b"the-document", pkcs7=pkcs7)
    assert result.status_code == 1
    assert result.subject_certificate is not None
    assert result.subject_certificate.serial_number == "SER-1"


@pytest.mark.asyncio
async def test_a_signature_over_other_bytes_is_status_minus_10():
    adapter = get_eimzo_adapter()
    pkcs7 = encode_mock_signature(
        document=b"the-document", serial="SER-1", issuer="ISS-1", pinfl="12345678901"
    )
    result = await adapter.verify_detached(document=b"OTHER-BYTES", pkcs7=pkcs7)
    assert result.status_code == -10
    assert EIMZO_STATUS_REASONS[-10] == "signature_invalid"


def test_every_documented_status_code_has_a_reason():
    # design/04 §2.5: the seven codes must not collapse into one message.
    assert set(EIMZO_STATUS_REASONS) == {1, -1, -5, -10, -11, -12, -20}


# The three methods below have no test in the brief's own Step 1 (only
# verify_detached and EIMZO_STATUS_REASONS are covered there); added so "every
# method" in the task title actually has coverage, not just the ones quoted.


@pytest.mark.asyncio
async def test_verify_attached_recovers_the_document_from_the_envelope():
    adapter = get_eimzo_adapter()
    pkcs7 = encode_mock_signature(
        document=b"the-document", serial="SER-1", issuer="ISS-1", pinfl="12345678901"
    )
    result = await adapter.verify_attached(pkcs7=pkcs7)
    assert result.status_code == 1
    assert result.subject_certificate is not None
    assert result.subject_certificate.serial_number == "SER-1"
    assert result.timestamp_token is not None


@pytest.mark.asyncio
async def test_verify_attached_still_matches_after_document_b64_is_dropped_from_raw():
    # Fix wave: `raw` must no longer carry `document_b64` (it round-tripped
    # the entire signed document into stored evidence), but the ATTACHED
    # path still needs it internally to recover the document from the
    # envelope in the first place -- this proves the fix did not disturb
    # that recovery, only what ends up in the returned `raw`.
    adapter = get_eimzo_adapter()
    pkcs7 = encode_mock_signature(
        document=b"the-document", serial="SER-1", issuer="ISS-1", pinfl="12345678901"
    )
    result = await adapter.verify_attached(pkcs7=pkcs7)
    assert result.status_code == 1  # the sha256 check inside still passed
    assert "document_b64" not in result.raw
    assert "document_sha256" in result.raw  # everything else in `raw` survives


@pytest.mark.asyncio
async def test_verify_detached_raw_never_carries_the_document_bytes():
    # The DETACHED path never needed `document_b64` to begin with (the
    # caller hands the document in separately) -- confirms the mock's own
    # envelope construction doesn't leak it here either.
    adapter = get_eimzo_adapter()
    pkcs7 = encode_mock_signature(
        document=b"the-document", serial="SER-1", issuer="ISS-1", pinfl="12345678901"
    )
    result = await adapter.verify_detached(document=b"the-document", pkcs7=pkcs7)
    assert result.status_code == 1
    assert "document_b64" not in result.raw


@pytest.mark.asyncio
async def test_garbage_pkcs7_is_a_verdict_not_an_exception():
    # verify_* reports a status code; it must not raise on malformed input the
    # way verify_signed_challenge does (that EimzoError is the unchanged login
    # contract, not this one).
    adapter = get_eimzo_adapter()
    detached = await adapter.verify_detached(document=b"x", pkcs7="garbage")
    attached = await adapter.verify_attached(pkcs7="garbage")
    assert detached.status_code == -10
    assert attached.status_code == -10
    assert detached.subject_certificate is None
    assert attached.subject_certificate is None


@pytest.mark.asyncio
async def test_certificate_status_active_by_default():
    adapter = get_eimzo_adapter()
    assert await adapter.certificate_status(serial="SER-1", issuer="ISS-1") == "active"


@pytest.mark.asyncio
async def test_certificate_status_revoked_serial_prefix():
    adapter = get_eimzo_adapter()
    assert await adapter.certificate_status(serial="REVOKED-1", issuer="ISS-1") == "revoked"


@pytest.mark.asyncio
async def test_certificate_status_expired_serial_prefix():
    adapter = get_eimzo_adapter()
    assert await adapter.certificate_status(serial="EXPIRED-1", issuer="ISS-1") == "expired"


@pytest.mark.asyncio
async def test_issue_challenge_returns_distinct_opaque_tokens():
    adapter = get_eimzo_adapter()
    first = await adapter.issue_challenge()
    second = await adapter.issue_challenge()
    assert isinstance(first, str) and len(first) > 16
    assert first != second
