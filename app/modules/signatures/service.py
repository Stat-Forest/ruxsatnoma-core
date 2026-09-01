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
from app.modules.signatures.verify import Verdict, build_verdict

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
    """Get-or-create the certificate `info` identifies, bound to `user` once
    ownership is proven — Task 4's own brief only checked the DB `user_id`;
    ruling 4's fuller text additionally requires the PINFL/TIN proof `auth`
    already applies to legal representations, and fix round 1 (ruling 1)
    closes that gap here, in the automatic path every `sign()` call takes.
    Task 7's explicit `POST /certificates` route enforces the same rule on
    its own, rarer path; leaving the proof there alone would mean an unknown
    `(serial_number, issuer)` pair is bound to whoever presents it first
    through THIS path, unchecked.

    Four outcomes:
    - unknown, and `info.pinfl_or_stir` is the caller's own PINFL -> create
      it, bound to `user`.
    - unknown, but `info.pinfl_or_stir` is NOT the caller's own PINFL ->
      create it anyway, as evidence that it was presented, but leave it
      UNBOUND (`user_id` stays `None`). This function has no document,
      purpose or object to hang a `signatures` row on, so it does not raise
      here — it returns the unbound row and lets `sign()`, which has that
      context, read `cert.user_id != user.id` off it and write the invalid
      signature row itself. An organisation certificate (a TIN rather than a
      personal PINFL) belongs to this same branch: proving it would need an
      effective-representation check, and `auth.service` exposes no usable
      entry point for that today (only `auth.repo.get_effective_representation`
      does, and reaching a sibling module's repo directly is exactly what
      "cross-module calls go through the other module's service" forbids) —
      flagged as BLOCKED rather than guessed at. Until that entry point
      exists, an organisation certificate is refused the same fail-closed way
      as any other PINFL mismatch.
    - known and not yet claimed by anyone (`user_id IS NULL`) -> the exact
      same first-bind decision as "unknown", by the exact same rule.
    - known and owned by someone else -> refuse. A person presenting a key
      that is not theirs is exactly as serious as a revoked certificate, so
      it takes the same evidence-then-raise path — except this function has
      no document or purpose to hang a `signatures` row on (its own
      signature carries neither), so its evidence is the audit entry alone:
      write it, commit, and only THEN raise, or the raise rolls the entry
      back with it.

    `unbound_at` (Task 7's column) plays a part in exactly one branch here:
    the signer IS this certificate's already-proven owner, re-signing with a
    key they had previously unbound from their own cabinet list. Unbinding is
    a convenience ("stop listing this key"), never a revocation — revocation
    is `status`, reconciled separately in `_reconcile_status`, and the
    verdict already refuses on it — so signing again simply re-lists the key
    (fix round 1, ruling 2). A certificate owned by someone else stays
    refused above regardless of its own `unbound_at`.
    """
    cert = await repo.get_certificate_by_identity(db, info.serial_number, info.issuer)
    if cert is None:
        cert = await repo.insert_certificate(db, info=info, user_id=None)
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
        if info.pinfl_or_stir == user.pinfl:
            cert.user_id = user.id
            await db.flush()
        # else: ownership unproven — leave `cert` unbound. `sign()` reads
        # `cert.user_id != user.id` off the return value and refuses there,
        # where it has the context to record the attempt as evidence.
    elif cert.unbound_at is not None:
        cert.unbound_at = None
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
    live status -> build the verdict -> if `bind_certificate` left the
    certificate unbound (ownership unproven, fix round 1 ruling 1), override
    the verdict to invalid with reason="certificate_pinfl_mismatch" — a
    crypto-valid signature from a certificate that is not provably the
    caller's own is still refused -> refuse a repeat of an already-signed
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
    if cert.user_id != user.id:
        # Ruling 4's fuller ownership proof (fix round 1, ruling 1):
        # `bind_certificate` left this certificate unbound rather than bind
        # it to `user` — see its own docstring for the full reasoning.
        # Ownership is a stronger gate than "the bytes verify", so this
        # overrides whatever `build_verdict` decided, and reuses the exact
        # same evidence-then-raise tail as every other invalid verdict below.
        verdict = Verdict(
            status="invalid",
            reason="certificate_pinfl_mismatch",
            record={**verdict.record, "reason": "certificate_pinfl_mismatch"},
        )

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
