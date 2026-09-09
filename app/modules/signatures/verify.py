"""The signature verdict — a pure function over one verification result.

No session, no I/O, no clock of its own: `now` is passed in only so a caller
can pin `checked_at`. The same shape as `norms/calculator.py`, and for the
same reason — the legal question "was this signature valid?" must be
answerable by hand, years later, from the stored record alone.

That self-containment is a promise this module keeps itself, not one it
borrows from the adapter: `record` copies the signing certificate's identity
(serial, issuer, subject, validity window) into its own explicit keys, read
from `result.subject_certificate` rather than from `result.raw`. `raw` is
whatever the adapter happened to receive — `design/04-integrations.md` §2.2
promises only a `subjectCertificateInfo` whose exact shape is the provider's
to change, so nothing about `raw`'s contents is a contract a later reader may
lean on. The five `certificate_*` keys are therefore always present, even as
`None` when `subject_certificate` is `None` (a verification that failed
before a certificate could be parsed) — a reader must be able to tell "there
was no certificate" from "this record predates the field", and only a
consistently shaped record allows that.

The same absence of a hard contract on the adapter's output is why
`build_verdict` fails closed rather than open: a `status_code` of `1` does
not by itself guarantee a certificate was ever parsed, so a missing
`subject_certificate` or `signed_at` reads as `"invalid"` /
`certificate_missing`, never as an unexamined `"valid"`."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.modules.integrations.adapters.eimzo import EIMZO_STATUS_REASONS, EimzoVerification


@dataclass(frozen=True)
class Verdict:
    """`status`/`reason` are the decision; `record` is `signatures.verification`
    verbatim (Task 4 writes it without further transformation) — every value in
    it is already a JSON primitive, per the lesson on `json.dumps` and `Decimal`/
    `date` (none appear here, but datetimes are stringified on the way in for the
    same reason). The five `certificate_*` keys carry the signing certificate's
    own identity, always present — `None`, never absent, when there was no
    certificate to read (see the module docstring)."""

    status: Literal["valid", "invalid"]
    reason: str | None
    record: dict[str, Any]


def build_verdict(result: EimzoVerification, *, cert_status: str, now: datetime) -> Verdict:
    """Five checks, in an order that is itself the ruling, not an accident of
    control flow:

    1. The adapter's own `status_code` — a cryptographically broken signature
       (bad hash, unparseable envelope, clock skew, an expired challenge) is
       broken regardless of what the certificate's standing is.
    2. The certificate's current status (`revoked`/`expired`) — looked up by
       the caller, passed in as `cert_status`, never re-derived here.
    3. Fails closed: nothing in `EimzoVerification` enforces that a
       `status_code` of `1` came with a certificate actually examined —
       `subject_certificate` and `signed_at` are each independently `| None`,
       and `design/04-integrations.md` §2.2 promises only a
       `subjectCertificateInfo` whose presence is the provider's to control.
       Either being `None` here is `certificate_missing`, not `"valid"`: a
       verifier that answers "valid" without ever looking at a certificate is
       the exact failure this module exists to prevent, and naming the
       missing piece keeps the failure recoverable in one retry.
    4. A trusted timestamp is mandatory (plan ruling R5, final review): with
       none, the only evidence of WHEN a document was signed is the signer's
       own computer clock, and a permit is a legal document with a validity
       period. `timestamp_token is None` is `timestamp_missing`, grouped with
       the other "evidence missing" checks above rather than the validity-
       window comparison below, which needs this evidence to already be in
       hand. Mock envelopes default `timestamp_token` to `"MOCK-TS"`
       (`encode_mock_signature`), so every existing mock-mode signing test
       already carries one and stays unaffected by this check.
    5. Only once all of the above are in hand: the validity window, compared
       against `signed_at` — **never against `now`** (plan ruling 5). A
       certificate that legitimately expires a year after a permit was
       signed must not retroactively invalidate that permit; the trusted
       timestamp is what makes that comparison meaningful.

    The first check that finds a problem wins; `reason` stays `None` (and
    `status` is `"valid"`) only if none of the five do."""
    cert = result.subject_certificate
    signed_at = result.signed_at
    reason: str | None = None
    if result.status_code != 1:
        reason = EIMZO_STATUS_REASONS.get(result.status_code, "signature_invalid")
    elif cert_status == "revoked":
        reason = "certificate_revoked"
    elif cert_status == "expired":
        reason = "certificate_expired"
    elif cert is None or signed_at is None:
        reason = "certificate_missing"
    elif result.timestamp_token is None:
        reason = "timestamp_missing"
    elif not (cert.valid_from <= signed_at <= cert.valid_to):
        reason = "certificate_invalid_at_signing"

    record: dict[str, Any] = {
        "status_code": result.status_code,
        "certificate_status": cert_status,
        "certificate_serial_number": cert.serial_number if cert else None,
        "certificate_issuer": cert.issuer if cert else None,
        "certificate_subject": cert.subject if cert else None,
        "certificate_valid_from": cert.valid_from.isoformat() if cert else None,
        "certificate_valid_to": cert.valid_to.isoformat() if cert else None,
        "timestamp_token": result.timestamp_token,
        "signed_at": signed_at.isoformat() if signed_at else None,
        "checked_at": now.isoformat(),
        "reason": reason,
        "raw": result.raw,
    }
    return Verdict(status="invalid" if reason else "valid", reason=reason, record=record)
