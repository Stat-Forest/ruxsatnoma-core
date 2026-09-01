"""E-IMZO adapter seam. The mock treats a base64url JSON envelope as a valid
signature or certificate (ruling 3), for both the login challenge and document
verification; real PKCS7/CRL verification against e-imzo-server is stage 5.2.
A legal-entity certificate carries the org STIR in `tin` (login) or
`pinfl_or_stir` (documents) alongside the signer's pinfl."""

import base64
import binascii
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from app.config import get_settings
from app.core.security import new_token
from app.modules.integrations.adapters.mock_codec import decode_payload, encode_payload


@dataclass(frozen=True)
class EimzoIdentity:
    challenge: str
    pinfl: str
    full_name: str
    tin: str | None = None
    legal_name: str | None = None
    cert_serial: str = "MOCK-CERT"
    cert_expires_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        if self.cert_expires_at is not None:
            data["cert_expires_at"] = self.cert_expires_at.isoformat()
        return data

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> EimzoIdentity:
        expires = data.get("cert_expires_at")
        parsed = datetime.fromisoformat(expires) if isinstance(expires, str) else None
        return cls(**{**data, "cert_expires_at": parsed})


class EimzoError(Exception):
    def __init__(self, err_code: str = "ERR-AUTH-004") -> None:
        super().__init__(err_code)
        self.err_code = err_code


# design/04 §2.5 — the seven E-IMZO status codes. `1` is success; every other
# code must keep its own machine-readable reason (ruling 9) rather than
# collapsing into one "signature error".
EIMZO_STATUS_REASONS: dict[int, str] = {
    1: "ok",
    -1: "certificate_status_unknown",
    -5: "clock_skew",
    -10: "signature_invalid",
    -11: "certificate_invalid",
    -12: "certificate_invalid_at_signing",
    -20: "challenge_expired",
}

CertificateStatus = Literal["active", "revoked", "expired"]


@dataclass(frozen=True)
class EimzoCertificateInfo:
    serial_number: str
    issuer: str
    subject: str
    pinfl_or_stir: str
    valid_from: datetime
    valid_to: datetime


@dataclass(frozen=True)
class EimzoVerification:
    status_code: int
    subject_certificate: EimzoCertificateInfo | None
    signed_at: datetime | None
    timestamp_token: str | None
    raw: dict[str, Any]


class EimzoAdapter(Protocol):
    async def verify_signed_challenge(self, signed_challenge: str) -> EimzoIdentity: ...

    async def issue_challenge(self) -> str: ...

    async def verify_attached(self, pkcs7: str) -> EimzoVerification: ...

    async def verify_detached(self, document: bytes, pkcs7: str) -> EimzoVerification: ...

    async def certificate_status(self, serial: str, issuer: str) -> CertificateStatus: ...


def encode_mock_signed_challenge(identity: EimzoIdentity) -> str:
    return encode_payload(identity.to_payload())


def encode_mock_signature(
    document: bytes,
    serial: str,
    issuer: str,
    pinfl: str,
    *,
    subject: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    signed_at: datetime | None = None,
    timestamp_token: str | None = "MOCK-TS",
) -> str:
    """Dev/test helper: a PKCS#7 stand-in over `document`, same base64url-JSON
    codec as the login mock (ruling 3). Carries the certificate fields, the
    signing time, the document itself (so `verify_attached` can recover it) and
    its sha256 (so `verify_detached` can check it without decoding the rest).
    Unset validity/signing times default to a window that brackets "now", so a
    caller that only names the four required fields still gets a valid signature."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "serial_number": serial,
        "issuer": issuer,
        "subject": subject or f"PINFL={pinfl}",
        "pinfl_or_stir": pinfl,
        "valid_from": (valid_from or now - timedelta(days=30)).isoformat(),
        "valid_to": (valid_to or now + timedelta(days=365)).isoformat(),
        "signed_at": (signed_at or now).isoformat(),
        "timestamp_token": timestamp_token,
        "document_b64": base64.b64encode(document).decode(),
        "document_sha256": hashlib.sha256(document).hexdigest(),
    }
    return encode_payload(payload)


def _unparseable_signature() -> EimzoVerification:
    return EimzoVerification(
        status_code=-10, subject_certificate=None, signed_at=None, timestamp_token=None, raw={}
    )


def _verify_envelope(pkcs7: str, document: bytes | None) -> EimzoVerification:
    """Shared verdict for verify_attached/verify_detached: decode the mock
    envelope and check the sha256 recorded at encoding time against the bytes
    that were actually signed. `document` is the caller-supplied bytes for the
    detached case; None means "recover them from the envelope" (attached).
    Any malformed input maps to -10 rather than raising — a verify call's job
    is to return a verdict, not to throw (unlike verify_signed_challenge, whose
    EimzoError is part of the existing, unchanged login contract)."""
    try:
        data = decode_payload(pkcs7)
        signed_bytes = document if document is not None else base64.b64decode(data["document_b64"])
        cert = EimzoCertificateInfo(
            serial_number=data["serial_number"],
            issuer=data["issuer"],
            subject=data["subject"],
            pinfl_or_stir=data["pinfl_or_stir"],
            valid_from=datetime.fromisoformat(data["valid_from"]),
            valid_to=datetime.fromisoformat(data["valid_to"]),
        )
        signed_at = datetime.fromisoformat(data["signed_at"])
    except ValueError, TypeError, KeyError, binascii.Error:
        return _unparseable_signature()
    matches = hashlib.sha256(signed_bytes).hexdigest() == data.get("document_sha256")
    return EimzoVerification(
        status_code=1 if matches else -10,
        subject_certificate=cert,
        signed_at=signed_at,
        timestamp_token=data.get("timestamp_token"),
        raw=data,
    )


class MockEimzo:
    async def verify_signed_challenge(self, signed_challenge: str) -> EimzoIdentity:
        try:
            return EimzoIdentity.from_payload(decode_payload(signed_challenge))
        except (ValueError, TypeError) as exc:
            raise EimzoError() from exc

    async def issue_challenge(self) -> str:
        """Ruling 11: in the real protocol the challenge belongs to e-imzo-server
        (`/frontend/challenge`), not to us. The mock only needs an opaque token in
        the same shape; `auth` still issues its own login challenges into its own
        token table through `auth.service.issue_eimzo_challenge`, unrelated to
        this call — that redirection, if it happens, is a later task's decision."""
        return new_token()

    async def verify_attached(self, pkcs7: str) -> EimzoVerification:
        return _verify_envelope(pkcs7, None)

    async def verify_detached(self, document: bytes, pkcs7: str) -> EimzoVerification:
        return _verify_envelope(pkcs7, document)

    async def certificate_status(self, serial: str, issuer: str) -> CertificateStatus:
        """`issuer` is accepted for parity with a real lookup (a serial number is
        only unique within its issuing CA) but unused by the mock: status is
        decided by a serial prefix convention — `REVOKED-` for `revoked`,
        `EXPIRED-` for `expired` (mirroring `REVOKED-`; design/04 §2.5 has no
        separate "expired" status code because a real CRL/OCSP check reports it
        directly), anything else is `active`."""
        if serial.startswith("REVOKED-"):
            return "revoked"
        if serial.startswith("EXPIRED-"):
            return "expired"
        return "active"


def get_eimzo_adapter() -> EimzoAdapter:
    if get_settings().eimzo_mode == "mock":
        return MockEimzo()
    raise NotImplementedError("real E-IMZO adapter arrives at stage 5.2")
