"""Signing service. `sign()` is the single method 3.9 (applications) and 3.11
(permits) call to attach a signature to anything — the rest of this module
exists to support it.

Level 2 (`models.py`'s own docstring): this module reaches `auth` only
through the `User` object a caller hands in, never queries `users` itself,
and reaches E-IMZO only through the `integrations.adapters.eimzo` seam
(`get_eimzo_adapter`; real verification arrives at stage 5.2)."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import EimzoCertificateInfo, get_eimzo_adapter
from app.modules.signatures import repo
from app.modules.signatures.models import Certificate, Signature
from app.modules.signatures.verify import build_verdict

# design/01 rule 6 / CLAUDE.md audit invariant: "<object>.<verb>" in English,
# the constant lives in the acting module. Every attempt against this action —
# a stored valid signature, a stored invalid one, or a certificate-ownership
# refusal with no signature row at all — shares this one code, so a reader can
# find every signing attempt against an object with one filter.
SIGNATURE_CREATE = "signature.create"


async def get_certificate(db: AsyncSession, certificate_id: uuid.UUID) -> Certificate:
    cert = await repo.get_certificate(db, certificate_id)
    if cert is None:
        raise err("ERR-SYS-003")
    return cert


async def get_for_object(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID
) -> list[Signature]:
    return await repo.list_for_object(db, object_type=object_type, object_id=object_id)


async def bind_certificate(
    db: AsyncSession, *, info: EimzoCertificateInfo, user: User
) -> Certificate:
    """Get-or-create the certificate `info` identifies, bound to `user`
    (ruling 4: bound on first use — the DB-only half of it: this is the
    automatic path a successful `sign()` takes, not the explicit `POST
    /certificates` registration route, which is where ruling 4's fuller
    PINFL/TIN ownership proof belongs).

    Three outcomes:
    - unknown -> create it, bound to `user`.
    - known and not yet claimed by anyone (`user_id IS NULL`) -> bind it to
      `user` now.
    - known and owned by someone else -> refuse. A person presenting a key
      that is not theirs is exactly as serious as a revoked certificate, so it
      takes the same evidence-then-raise path — except this function has no
      document or purpose to hang a `signatures` row on (its own signature
      carries neither), so its evidence is the audit entry alone: write it,
      commit, and only THEN raise, or the raise rolls the entry back with it.

    `unbound_at` (Task 7's column) plays no part in any of this. Task 7's own
    unbind route sets ONLY `unbound_at`, never `user_id` — pre-flight ruling
    P3 draws that line on purpose (`status` is the certificate's PKI state,
    `unbound_at` is our own list-membership state, and a certificate a
    signature references must keep resolving forever). So a certificate the
    owner unbound still has `user_id` pointing at them, still reaches the
    "owned by this user" branch below untouched, and signs exactly as before
    — nothing here "resurrects" a binding, because none was ever cleared.
    Revocation, the one thing that SHOULD stop a certificate from signing
    again, is `status`, reconciled separately in `_reconcile_status`.
    """
    cert = await repo.get_certificate_by_identity(db, info.serial_number, info.issuer)
    if cert is None:
        return await repo.insert_certificate(db, info=info, user_id=user.id)
    if cert.user_id is not None and cert.user_id != user.id:
        await audit.log(
            db,
            action=SIGNATURE_CREATE,
            user_id=user.id,
            object_type="certificate",
            object_id=cert.id,
            result="denied",
            basis="certificate_owned_by_another_user",
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": "certificate_owned_by_another_user"})
    if cert.user_id is None:
        cert.user_id = user.id
        await db.flush()
    return cert


async def _reconcile_status(db: AsyncSession, cert: Certificate, live_status: str) -> None:
    """The adapter's CRL/OCSP-equivalent answer is the truth about a
    certificate's PKI state; the `status` we stored at bind time (or last
    reconciled) can go stale the moment the CA revokes a certificate someone
    already holds. Reconciled on every sign attempt, not on a schedule — the
    moment that matters is the one about to decide a verdict."""
    if live_status == cert.status:
        return
    cert.status = live_status
    if live_status == "revoked" and cert.revoked_at is None:
        cert.revoked_at = datetime.now(UTC)
    await db.flush()


async def sign(
    db: AsyncSession,
    *,
    object_type: str,
    object_id: uuid.UUID,
    purpose: str,
    document: bytes,
    pkcs7: str,
    user: User,
) -> Signature:
    """Attach a signature to `(object_type, object_id, purpose)`.

    Order: hash the bytes WE were handed (never trust the envelope's own claim
    of what it signed) -> verify -> bind the certificate the envelope names
    (may itself refuse and raise — see `bind_certificate`) -> reconcile its
    live status -> build the verdict -> refuse a repeat of an already-signed
    purpose, checked only once we know this attempt would otherwise have been
    valid (an invalid attempt can never occupy the slot — `uq_signatures_valid_
    purpose` only covers `verification_status = 'valid'` rows, so checking
    duplicates ahead of an attempt that was going to fail on its own merits
    would report the wrong reason) -> insert the row ALWAYS, valid or not
    (ruling 8: a failed attempt is evidence) -> audit -> if the verdict itself
    is invalid, commit that evidence and only THEN raise.
    """
    doc_hash = hashlib.sha256(document).hexdigest()
    adapter = get_eimzo_adapter()
    result = await adapter.verify_detached(document=document, pkcs7=pkcs7)
    info = result.subject_certificate

    if info is None:
        # Nothing to resolve a certificate_id from — `signatures.certificate_id`
        # is NOT NULL, so this is the one case where "insert always" cannot
        # literally hold. `build_verdict` still names a reason (its own
        # status_code/cert-missing checks do not need a certificate to run);
        # `cert_status` is unused on this path since `status_code != 1` (the
        # only way this adapter reaches here) always wins ahead of it. The
        # attempt is still audited and committed before raising, same as
        # every other refusal below — there is just no signature row to keep
        # alongside it.
        verdict = build_verdict(result, cert_status="active", now=datetime.now(UTC))
        await audit.log(
            db,
            action=SIGNATURE_CREATE,
            user_id=user.id,
            object_type=object_type,
            object_id=object_id,
            result="denied",
            basis=verdict.reason,
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": verdict.reason})

    cert = await bind_certificate(db, info=info, user=user)
    live_status = await adapter.certificate_status(serial=info.serial_number, issuer=info.issuer)
    await _reconcile_status(db, cert, live_status)

    verdict = build_verdict(result, cert_status=cert.status, now=datetime.now(UTC))

    if verdict.status == "valid":
        # Only a would-be-valid insert can conflict with the partial unique
        # index (lesson: a partial index only constrains the rows it covers) —
        # an attempt that is invalid on its own merits is reported as such,
        # never as a duplicate.
        if await repo.get_valid_signature(db, object_type, object_id, purpose) is not None:
            raise err("ERR-SIGN-002")

    try:
        signature = await repo.insert_signature(
            db,
            object_type=object_type,
            object_id=object_id,
            purpose=purpose,
            signer_user_id=user.id,
            certificate_id=cert.id,
            doc_hash=doc_hash,
            signature_value=pkcs7,
            # `info is not None` guarantees `signed_at is not None` too in
            # every adapter implementation so far (both are resolved together
            # or not at all — see eimzo.py's `_verify_envelope`); the fallback
            # only guards the column's NOT NULL against a future adapter that
            # breaks that pairing. `verdict.record["signed_at"]` still carries
            # the honest `None` in that case — this is a column fallback only.
            signed_at=result.signed_at or datetime.now(UTC),
            verification=verdict.record,
            verification_status=verdict.status,
        )
    except IntegrityError as exc:
        # IntegrityError IS a DBAPIError subclass — this narrow clause must be
        # checked first (lesson) — the service-level check above just lost a
        # race to a concurrent sign() for the same purpose.
        raise err("ERR-SIGN-002") from exc

    await audit.log(
        db,
        action=SIGNATURE_CREATE,
        user_id=user.id,
        object_type=object_type,
        object_id=object_id,
        result="success" if verdict.status == "valid" else "denied",
        basis=verdict.reason,
    )

    if verdict.status == "invalid":
        # Early-commit pattern: the failed attempt IS the evidence (ruling 8)
        # and stage 4.2's risk reporting reads it — a raise before this commit
        # would roll the row and the audit entry back together with the very
        # exception they exist to explain.
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": verdict.reason})

    return signature
