"""The signature verdict — a pure function over one verification result.

No session, no I/O, no clock of its own: `now` is passed in only so a caller
can pin `checked_at`. The same shape as `norms/calculator.py`, and for the
same reason — the legal question "was this signature valid?" must be
answerable by hand, years later, from the stored record alone."""

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
    same reason)."""

    status: Literal["valid", "invalid"]
    reason: str | None
    record: dict[str, Any]


def build_verdict(result: EimzoVerification, *, cert_status: str, now: datetime) -> Verdict:
    """Three checks, in an order that is itself the ruling, not an accident of
    control flow:

    1. The adapter's own `status_code` — a cryptographically broken signature
       (bad hash, unparseable envelope, clock skew, an expired challenge) is
       broken regardless of what the certificate's standing is.
    2. The certificate's current status (`revoked`/`expired`) — looked up by
       the caller, passed in as `cert_status`, never re-derived here.
    3. The validity window, compared against `result.signed_at` — **never
       against `now`** (plan ruling 5). A certificate that legitimately expires
       a year after a permit was signed must not retroactively invalidate that
       permit; the trusted timestamp is what makes that comparison meaningful.

    The first check that finds a problem wins; `reason` stays `None` (and
    `status` is `"valid"`) only if none of the three do."""
    reason: str | None = None
    if result.status_code != 1:
        reason = EIMZO_STATUS_REASONS.get(result.status_code, "signature_invalid")
    elif cert_status == "revoked":
        reason = "certificate_revoked"
    elif cert_status == "expired":
        reason = "certificate_expired"
    elif result.subject_certificate is not None and result.signed_at is not None:
        cert = result.subject_certificate
        if not (cert.valid_from <= result.signed_at <= cert.valid_to):
            reason = "certificate_invalid_at_signing"

    record: dict[str, Any] = {
        "status_code": result.status_code,
        "certificate_status": cert_status,
        "timestamp_token": result.timestamp_token,
        "signed_at": result.signed_at.isoformat() if result.signed_at else None,
        "checked_at": now.isoformat(),
        "reason": reason,
        "raw": result.raw,
    }
    return Verdict(status="invalid" if reason else "valid", reason=reason, record=record)
