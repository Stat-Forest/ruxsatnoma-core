"""Parsing e-imzo-server's JSON into this codebase's dataclasses.

Split out of `eimzo.py` so the transport file stays about HTTP. Everything
here is pure: no I/O, no settings, no session — which is what makes the
vendor's own response samples (`tests/modules/integrations/eimzo_samples.py`)
enough to test the whole of it.

Verified against e-imzo-server v2.1.1 (the vendor's own `/backend/auth`
sample, published 2026-05-25, and a `javap` read of the unpacked jar's
`uz/eimzo/server/json/` package) and the official README at
github.com/qo0p/e-imzo-doc."""

from datetime import UTC, datetime
from typing import Any

from app.modules.integrations.adapters.eimzo import EimzoCertificateInfo, EimzoVerification

# The person's PINFL and the organization's STIR, as OIDs in a certificate
# subject map — `/backend/auth`'s `subjectName` and a pkcs7 signer
# certificate's `subjectInfo` are both this same OID-keyed shape.
OID_PINFL = "1.2.860.3.16.1.2"
OID_LEGAL_TIN = "1.2.860.3.16.1.1"


def parse_provider_datetime(value: str) -> datetime:
    """`"2026-05-25 15:47:22"` — a space, no timezone.

    `fromisoformat` parses this shape happily and returns a NAIVE datetime,
    which then compares against `datetime.now(UTC)` in `signatures.verify`
    and raises `TypeError` at the worst possible moment. The provider's
    times are Tashkent-server times expressed as UTC; stamping UTC here is
    the one place that decision is made. Every date this module reads —
    certificate validity, signing time, the trusted timestamp's own time —
    goes through this one function, on purpose."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_subject(subject_name: dict[str, str]) -> tuple[str, str, str | None]:
    """The certificate subject as an OID-keyed map (`design/04` §2.2 step 5).

    Returns `(human-readable subject, the identifier ownership is proven
    against, the organization STIR if this is a legal-entity certificate)`.
    The PERSONAL PINFL wins whenever both are present: `signatures.service.
    _ownership_reason` compares the identifier against the signed-in user's
    own `users.pinfl`, and a legal-entity certificate still names the human
    who holds it. With no personal PINFL at all (a legal-entity-only
    certificate), the org STIR is what `pinfl_or_stir` falls back to — some
    identifier has to be there for that same ownership check to compare
    against."""
    pinfl = subject_name.get(OID_PINFL, "").strip()
    legal_tin = subject_name.get(OID_LEGAL_TIN, "").strip() or None
    common_name = subject_name.get("CN", "").strip()
    organization = subject_name.get("O", "").strip()
    subject = ", ".join(part for part in (common_name, organization) if part)
    return subject, (pinfl or legal_tin or ""), legal_tin


def _certificate_from_pkcs7_entry(cert_data: dict[str, Any]) -> EimzoCertificateInfo | None:
    """A pkcs7-verify signer's own `certificate[0]` — a different shape from
    `/backend/auth`'s `subjectCertificateInfo` (`subjectInfo` rather than
    `subjectName`, and a real `issuerName` rather than needing `X500Name` as
    a stand-in).

    Returns `None` on a malformed entry — a missing `serialNumber`/
    `validFrom`/`validTo`, or a date string `parse_provider_datetime` cannot
    parse — rather than raising (finding 4, final review): a provider
    payload the vendor itself calls successful (`status: 1`) but that is
    missing a field this codebase reads must become a verdict
    (`certificate_missing`, via `build_verdict`), never a `KeyError`/
    `ValueError` escaping `sign()`'s `except EimzoError` as an unhandled
    500 with no signature row, no audit entry and no integration-log row."""
    try:
        subject, identifier, _legal_tin = read_subject(cert_data.get("subjectInfo") or {})
        return EimzoCertificateInfo(
            serial_number=cert_data["serialNumber"],
            issuer=cert_data.get("issuerName", ""),
            subject=subject,
            pinfl_or_stir=identifier,
            valid_from=parse_provider_datetime(cert_data["validFrom"]),
            valid_to=parse_provider_datetime(cert_data["validTo"]),
        )
    except KeyError, ValueError, TypeError:
        return None


def _verified_status(signer: dict[str, Any], status: int) -> int:
    """Ruling FR-1 (final review): the vendor's OUTER `status` may mean only
    "request processed", not "signature good" — `pkcs7Info.signers[0]` itself
    carries three independent verification booleans (`verified`,
    `certificateVerified`, `certificateValidAtSigningTime`, all present in
    the vendor's own sample) that a `status` of `1` does not by itself
    guarantee. An explicit `False` on any of them overrides a `status` of `1`
    with the EXISTING status code (and, through it, `EIMZO_STATUS_REASONS`'s
    existing reason) that already means the same thing — `build_verdict`
    needs no change at all for this. An ABSENT boolean (the key missing, or
    `None`) changes nothing: a provider that never sends one of these fields
    must not have every signature it approves flip to invalid — only an
    EXPLICIT `False` refuses."""
    if status != 1:
        return status
    if signer.get("verified") is False:
        return -10  # EIMZO_STATUS_REASONS[-10] == "signature_invalid"
    if signer.get("certificateVerified") is False:
        return -11  # EIMZO_STATUS_REASONS[-11] == "certificate_invalid"
    if signer.get("certificateValidAtSigningTime") is False:
        return -12  # EIMZO_STATUS_REASONS[-12] == "certificate_invalid_at_signing"
    return status


def verification_from_pkcs7_info(data: dict[str, Any]) -> EimzoVerification:
    """Reads `pkcs7Info.signers[0]` — one document, one signer, mirroring
    what `EimzoVerification` itself carries (a single `subject_certificate`,
    a single `signed_at`).

    `signingTime` and `timeStampInfo.time` both cross the naive-datetime trap
    documented on `parse_provider_datetime` and go through it; the trusted
    timestamp's own time is what ends up in `timestamp_token`, normalized to
    an aware ISO string rather than the ambiguous string the provider sent.

    `raw` is `pkcs7Info` with `documentBase64` dropped before anything is
    stored (`eimzo.py::_verify_envelope`'s docstring: the same contract, the
    same reason — an append-only column served whole to every co-signer must
    never carry the signed document itself) and, per signer, a flattened
    `paramSetOID` copied up from `certificate[0].publicKey` — the one field
    `signatures` evidence wants at hand without reaching back into the
    certificate chain for it.

    Never raises: a totally failed verification may arrive with no
    `pkcs7Info` at all (the provider's other response shape, keyed on
    `failedSignerInfo` instead) — every lookup here is defensive, and that
    shape simply yields an "empty" verdict with only `status_code` filled
    in, exactly like `eimzo.py::_unparseable_signature`. Nor does a
    `status: 1` response with a malformed certificate entry or an
    unparseable date raise (finding 4, final review): `_certificate_from_
    pkcs7_entry` and the two date reads below each fail closed to `None`
    rather than letting `KeyError`/`ValueError` escape — a provider payload
    this codebase cannot fully read becomes a verdict
    (`certificate_missing`/`timestamp_missing` in `build_verdict`), never an
    unhandled 500 with no signature row, no audit entry and no
    integration-log row."""
    pkcs7_info = data.get("pkcs7Info") or {}
    signers = pkcs7_info.get("signers") or []
    signer = signers[0] if signers else {}

    certificates = signer.get("certificate") or []
    cert_data = certificates[0] if certificates else None
    subject_certificate = _certificate_from_pkcs7_entry(cert_data) if cert_data else None

    signing_time = signer.get("signingTime")
    try:
        signed_at = parse_provider_datetime(signing_time) if signing_time else None
    except ValueError, TypeError:
        signed_at = None

    timestamp_info = signer.get("timeStampInfo") or {}
    timestamp_time = timestamp_info.get("time")
    try:
        timestamp_token = (
            parse_provider_datetime(timestamp_time).isoformat() if timestamp_time else None
        )
    except ValueError, TypeError:
        timestamp_token = None

    raw_signers = []
    for entry in signers:
        entry_copy = dict(entry)
        entry_certificates = entry.get("certificate") or []
        if entry_certificates and "publicKey" in entry_certificates[0]:
            entry_copy["paramSetOID"] = entry_certificates[0]["publicKey"].get("paramSetOID")
        raw_signers.append(entry_copy)
    raw = {k: v for k, v in pkcs7_info.items() if k != "documentBase64"}
    if "signers" in pkcs7_info:
        raw["signers"] = raw_signers

    return EimzoVerification(
        status_code=_verified_status(signer, data.get("status", 0)),
        subject_certificate=subject_certificate,
        signed_at=signed_at,
        timestamp_token=timestamp_token,
        raw=raw,
    )
