"""Signing service. `sign()` is the single method 3.9 (applications) and 3.11
(permits) call to attach a signature to anything — the rest of this module
exists to support it.

Level 2 (`models.py`'s own docstring): this module reaches `auth` through the
`User` object a caller hands in and, since fix round 2, through
`auth.service.has_effective_representation` (proving an organisation
certificate belongs to its presenter) — never queries `users`/
`representations` itself, only ever through `auth`'s own service. Reaches
E-IMZO only through the `integrations.adapters.eimzo` seam
(`get_eimzo_adapter`; real verification arrives at stage 5.2)."""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth import service as auth_service
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
    already applies to legal representations. Fix round 1 (ruling 1) closed
    the personal-PINFL half; fix round 2 closes the organisation-TIN half —
    both live in `_ownership_reason`, in the automatic path every `sign()`
    call takes. Task 7's explicit `POST /certificates` route enforces the
    same rule on its own, rarer path; leaving the proof there alone would
    mean an unknown `(serial_number, issuer)` pair is bound to whoever
    presents it first through THIS path, unchecked.

    Four outcomes:
    - unknown, and `_ownership_reason` proves `info` is the caller's own ->
      create it, bound to `user`.
    - unknown, but `_ownership_reason` cannot prove it belongs to the caller
      (a stranger's personal PINFL, a signer with no recorded PINFL of their
      own, or an organisation STIR the caller holds no effective
      representation for — see that function's own docstring for the three
      proof shapes) -> create it anyway, as evidence that it was presented,
      but leave it UNBOUND (`user_id` stays `None`). This function has no
      document, purpose or object to hang a `signatures` row on, so it does
      not raise here — it returns the unbound row and lets `sign()`, which
      has that context, re-run `_ownership_reason` itself (fix round 3, fix
      2: on EVERY call, not only a first bind) and write the invalid
      signature row.
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
    (fix round 1, ruling 2). This branch does NOT re-run `_ownership_reason`:
    `cert.user_id == user.id` here is a purely administrative fact (who
    first bound this row), unrelated to whether a signature may be produced
    with it RIGHT NOW — `sign()` is the one place that re-proves ownership on
    every call (fix round 3, fix 2), so a cert may legitimately be re-listed
    here and still refused a moment later in `sign()` if e.g. its
    representation has since expired. A certificate owned by someone else
    stays refused above regardless of its own `unbound_at`.
    """
    cert = await repo.get_certificate_by_identity(db, info.serial_number, info.issuer)
    if cert is None:
        try:
            cert = await repo.insert_certificate(db, info=info, user_id=None)
        except IntegrityError as exc:
            # `uq_certificate_identity`: two callers presenting the same
            # brand-new (serial_number, issuer) at once can both pass the
            # SELECT above before either commits its own INSERT — the exact
            # shape create_contour's own number-taken race documents
            # (gis/service.py). `user_id=None` here is a literal, never a
            # caller-supplied FK, and `info`'s own fields were already
            # resolved by the adapter before this call — the ONE violation
            # left to catch is this identity race (review fix 5). Mapped to a
            # domain error rather than a 500, same shape as that precedent:
            # no audit call here (unlike sign()'s own ERR-SIGN-002 paths,
            # fix 1) — the failed flush already aborted the transaction and
            # writing evidence would need a rollback first, which this
            # narrower fix does not take on; raise immediately, no further
            # `db` use on this path, and get_db's rollback-on-exception
            # clears the aborted transaction before this becomes a response.
            raise err("ERR-SIGN-004", details={"reason": "certificate_conflict"}) from exc
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
        if await _ownership_reason(db, info=info, user=user) is None:
            cert.user_id = user.id
            await db.flush()
        # else: ownership unproven — leave `cert` unbound. `sign()` re-runs
        # `_ownership_reason` itself and refuses there, where it has the
        # context to record the attempt as evidence.
    elif cert.unbound_at is not None:
        cert.unbound_at = None
        await db.flush()
    return cert


async def _ownership_reason(
    db: AsyncSession, *, info: EimzoCertificateInfo, user: User
) -> str | None:
    """Whether `user` can prove `info` is their own key, right now. Returns
    `None` when ownership is proven, otherwise the `details.reason` the
    caller should refuse with — one function decides both WHETHER and WHY,
    so the two can never drift the way a bool-returning predicate and a
    second, separately-maintained reason-picker would (fix round 3, fix 3).
    Called both from `bind_certificate` (a first-bind opportunity) AND from
    `sign()` on EVERY call (fix round 3, fix 2) — never cached on the
    certificate row, since a representation can expire or be revoked after
    the certificate was first bound, and `cert.user_id` alone cannot see
    that happen.

    `EimzoCertificateInfo` carries no explicit personal/org kind flag, so the
    shape of `pinfl_or_stir` itself is the only signal available: 14 digits is
    a personal PINFL (`users.pinfl`'s own `^[0-9]{14}$` CHECK), 9 digits is an
    organisation STIR (`applicants.stir`'s own `^[0-9]{9}$` CHECK) — the two
    formats never collide, so the length alone disambiguates them.

    - Organisation (9 digits): proven the way `auth` already proves legal
      representation — the caller must hold an EFFECTIVE representation for
      that STIR right now (fix round 2). `"certificate_pinfl_mismatch"` when
      they don't — a stranger's certificate and one for a STIR whose
      representation has since expired or been revoked read the same way to
      a caller: "not provably yours right now".
    - Personal (14 digits): it must equal the caller's own `user.pinfl`.
      `user.pinfl` can itself be `None` — a staff user created before their
      PINFL was recorded (real: `users.pinfl` is nullable) — and that is OUR
      data gap, not a wrong certificate, so it gets its own honest reason,
      `"signer_pinfl_unknown"` (fix round 3, fix 3), never the generic
      mismatch used for an actual stranger's certificate.
    """
    if len(info.pinfl_or_stir) == 9:
        if await auth_service.has_effective_representation(
            db, user_id=user.id, stir=info.pinfl_or_stir
        ):
            return None
        return "certificate_pinfl_mismatch"
    if user.pinfl is None:
        return "signer_pinfl_unknown"
    if info.pinfl_or_stir == user.pinfl:
        return None
    return "certificate_pinfl_mismatch"


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

    TRANSACTION CONTRACT — read this before calling `sign()` from a larger
    unit of work: every refusal below commits before it raises (the
    early-commit pattern this codebase uses throughout, so the evidence
    survives the very exception that explains it — ruling 8). That commit is
    on the CALLER's own `db` session and commits EVERYTHING pending on it,
    not only what `sign()` itself wrote. A caller that, say, creates a
    permit and only THEN calls `sign()` risks a REFUSED signature leaving
    that half-built permit committed and durable, not rolled back together
    with the refusal it accompanies. Call `sign()` before creating any
    dependent state you would not want persisted on a refusal, or commit
    your own prerequisite state first and treat `sign()` as its own
    transaction boundary (fix round 3, fix 4).

    Order: hash the bytes WE were handed (never trust the envelope's own claim
    of what it signed) -> verify -> bind the certificate the envelope names
    (may itself refuse and raise — see `bind_certificate`) -> reconcile its
    live status -> build the verdict -> re-prove ownership via
    `_ownership_reason`, on EVERY call, not only trusted once from
    `bind_certificate`'s own stored `user_id` (fix round 3, fix 2 — a
    representation can expire or be revoked after the certificate was first
    bound, and a fast path that skips this re-proof would keep authorising
    signatures with it forever); when unproven, override the verdict to
    invalid with that reason (`"certificate_pinfl_mismatch"` or, when the
    gap is OUR OWN missing PINFL for this signer, `"signer_pinfl_unknown"` —
    fix round 3, fix 3) — a crypto-valid signature from a certificate that is
    not provably the caller's own right now is still refused -> refuse a
    repeat of an already-signed purpose, checked only once we know this
    attempt would otherwise have been valid (an invalid attempt can never
    occupy the slot — `uq_signatures_valid_purpose` only covers
    `verification_status = 'valid'` rows, so checking duplicates ahead of an
    attempt that was going to fail on its own merits would report the wrong
    reason), itself evidence-then-raise on a hit (fix round 3, fix 1 — a
    duplicate is a double-click far more often than an attack, and this
    audits the attempt without a second `signatures` row) -> insert the row
    ALWAYS, valid or not (ruling 8: a failed attempt is evidence) -> audit ->
    if the verdict itself is invalid, commit that evidence and only THEN
    raise.
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
    unowned_reason = await _ownership_reason(db, info=info, user=user)
    if unowned_reason is not None:
        # Ruling 4's fuller ownership proof (fix round 1 ruling 1; the
        # organisation-STIR half closed in fix round 2), re-run HERE on
        # EVERY sign() call (fix round 3, fix 2) rather than trusted once
        # from `bind_certificate`'s own stored `cert.user_id` — that fast
        # path never re-derives ownership once it is set, so a
        # representation that expires or is revoked AFTER the certificate
        # was first bound would otherwise keep authorising signatures with
        # it forever, defeating the entire point of time-boxing it.
        # Ownership is a stronger gate than "the bytes verify", so this
        # overrides whatever `build_verdict` decided, and reuses the exact
        # same evidence-then-raise tail as every other invalid verdict below.
        verdict = Verdict(
            status="invalid",
            reason=unowned_reason,
            record={**verdict.record, "reason": unowned_reason},
        )

    if verdict.status == "valid":
        # Only a would-be-valid insert can conflict with the partial unique
        # index (lesson: a partial index only constrains the rows it covers) —
        # an attempt that is invalid on its own merits is reported as such,
        # never as a duplicate.
        if await repo.get_valid_signature(db, object_type, object_id, purpose) is not None:
            # Evidence-then-raise (fix round 3, fix 1), same shape as every
            # other refusal here: audit it, but write no `signatures` row —
            # a duplicate is a double-click far more often than an attack,
            # and a row per retry would fill the evidence table with noise
            # stage 4.2 then has to filter back out. The audit entry alone
            # answers "who tried and when", which is all this event is worth.
            await audit.log(
                db,
                action=SIGNATURE_CREATE,
                user_id=user.id,
                object_type=object_type,
                object_id=object_id,
                result="denied",
                basis="already_signed",
            )
            await db.commit()
            raise err("ERR-SIGN-002")

    try:
        # `begin_nested()` (a SAVEPOINT), not a bare call: this insert can
        # lose a race (see the `except` below), and recovering from THAT
        # failure still needs to write an audit entry and commit on this SAME
        # session afterward (fix round 3, fix 1). A bare `db.flush()` here,
        # caught by `except IntegrityError` with a plain `db.rollback()`,
        # verified experimentally to roll back the WHOLE transaction — not
        # just this insert's own failed statement, but every OTHER thing the
        # caller had flushed-but-not-committed before ever calling `sign()`
        # (the exact half-built state the transaction contract above warns a
        # caller about, destroyed silently instead of committed). A SAVEPOINT
        # scopes the rollback to just this block: on failure, only what this
        # `async with` wrote is undone, and everything flushed before it
        # remains intact and usable.
        async with db.begin_nested():
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
                # every adapter implementation so far (both are resolved
                # together or not at all — see eimzo.py's `_verify_envelope`);
                # the fallback only guards the column's NOT NULL against a
                # future adapter that breaks that pairing.
                # `verdict.record["signed_at"]` still carries the honest
                # `None` in that case — this is a column fallback only.
                signed_at=result.signed_at or datetime.now(UTC),
                verification=verdict.record,
                verification_status=verdict.status,
            )
    except IntegrityError as exc:
        # IntegrityError IS a DBAPIError subclass — this narrow clause must be
        # checked first (lesson). Only `uq_signatures_valid_purpose`'s own
        # violation means "already signed" (fix round 3, fix 6) — the
        # `certificate_id` FK and the `verification_status` CHECK are
        # integrity violations on this SAME insert too, and mislabeling
        # either "already signed" would report an untrue reason for a real
        # defect. SQLAlchemy's asyncpg dialect always re-raises `from` the
        # driver's own exception (`raise translated_error from error` in
        # sqlalchemy.dialects.postgresql.asyncpg), so the object that
        # actually carries WHICH constraint fired is `exc.orig.__cause__`
        # (asyncpg's own UniqueViolationError/CheckViolationError/...), never
        # `exc.orig` itself — that is only SQLAlchemy's thin DBAPI wrapper
        # and exposes `pgcode`/`sqlstate` alone (verified empirically against
        # a real violation of each of this table's three constraint kinds —
        # UNIQUE, CHECK, FK — all three populate `constraint_name` the same
        # way). Anything other than the one constraint this except exists for
        # surfaces as itself, unmapped — the `begin_nested()` above already
        # undid the failed insert either way, so a bare `raise` here leaves
        # `db` exactly as it was before this whole `try` started.
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_signatures_valid_purpose":
            raise
        # The service-level check above just lost a race to a concurrent
        # sign() for the same purpose — evidence, same as every other
        # refusal (fix round 3, fix 1): a duplicate is a double-click far
        # more often than an attack, and this answers "who tried and when"
        # without a second `signatures` row.
        await audit.log(
            db,
            action=SIGNATURE_CREATE,
            user_id=user.id,
            object_type=object_type,
            object_id=object_id,
            result="denied",
            basis="already_signed",
        )
        await db.commit()
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
