"""Signing service. `sign()` is the single method 3.9 (applications) and 3.11
(permits) call to attach a signature to anything — the rest of this module
exists to support it.

Level 2 (`models.py`'s own docstring): this module reaches `auth` through the
`User` object a caller hands in and, since fix round 2, through
`auth.service.has_effective_representation` (proving an organisation
certificate belongs to its presenter) — never queries `users`/
`representations` itself, only ever through `auth`'s own service. Reaches
E-IMZO only through the `integrations.adapters.eimzo` seam
(`get_eimzo_adapter`; real verification arrives at stage 5.2). Task 7 adds
one further, narrow exception: `auth.repo.role_code`/`permission_codes`,
read directly rather than through `auth`'s service, for a permission check
that must run INSIDE a handler rather than as a route-level
`require_permission` dependency (`_holds_view_any`, below) — the same
`auth_repo` import `norms.service`/`gis.service`/`admin.users_service`
already use for the identical shape of check."""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.errors import err
from app.core.schemas import PageParams
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth import service as auth_service
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import (
    EIMZO_STATUS_REASONS,
    EimzoCertificateInfo,
    get_eimzo_adapter,
)
from app.modules.signatures import repo
from app.modules.signatures.models import Certificate, Signature
from app.modules.signatures.permissions import VIEW_ANY
from app.modules.signatures.verify import Verdict, build_verdict

# design/01 rule 6 / CLAUDE.md audit invariant: "<object>.<verb>" in English,
# the constant lives in the acting module. Every attempt against this action —
# a stored valid signature, a stored invalid one, or a certificate-ownership
# refusal with no signature row at all — shares this one code, so a reader can
# find every signing attempt against an object with one filter.
SIGNATURE_CREATE = "signature.create"

# Task 7's own three actions — distinct from SIGNATURE_CREATE because none of
# them is a signing attempt: binding a certificate ahead of time, unbinding
# one, and oversight re-verification are their own events with their own
# "<object>.<verb>" codes, so a reader filtering "every signing attempt" by
# SIGNATURE_CREATE alone is not also handed unrelated certificate-lifecycle
# noise. `bind_certificate`'s OWN internal "owned by another user" refusal
# (below) keeps using SIGNATURE_CREATE regardless of which caller reached it
# — that behaviour is Task 4's, already relied on by `sign()`'s tests, and
# this task does not change it.
CERTIFICATE_BIND = "certificate.bind"
CERTIFICATE_UNBIND = "certificate.unbind"
SIGNATURE_REVERIFY = "signature.reverify"

# RI-05 (docs/tz/10-klassifikatory.md): "an attempt to sign with a revoked or
# expired certificate", severity high. `build_verdict` (verify.py) also
# reports "certificate_invalid_at_signing" for a certificate that was outside
# its own validity window at the moment it signed -- the same
# certificate-standing failure the classifier names, just caught by the
# validity-window check rather than the live revoked/expired status, so it
# counts too. Deliberately excludes the two ownership reasons
# ("certificate_pinfl_mismatch", "signer_pinfl_unknown" -- a stranger's or an
# unrecorded PINFL, not a bad certificate) and "signature_invalid" (a broken
# signature, not a certificate problem): marking those would flood stage
# 4.2's risk report (not built yet -- this task only marks the event) with
# the wrong events. Public (not `_`-prefixed) so a reader -- a test, or
# stage 4.2 itself -- has one place to check "does this reason count",
# rather than a second, separately maintained copy of the three strings.
CERTIFICATE_STANDING_REASONS = frozenset(
    {"certificate_revoked", "certificate_expired", "certificate_invalid_at_signing"}
)

# Which `system_settings` key configures the required-purposes list for a
# given object type -- a dict, not an `if`-chain, so a future object type
# (3.9's own applications, say) is a data change here plus one new
# `SettingSpec` in settings_store.py, not a new branch of code. No entry
# means no configured requirement (see `required_purposes` below), not a bug:
# most object types have no signature requirement at all.
_REQUIREMENT_SETTINGS: dict[str, str] = {
    "permit": "permit_required_signatures",
}


def _ri05_extra(reason: str | None) -> dict[str, Any] | None:
    """The `extra=` value for an `audit.log` call reporting `reason` on a
    signing attempt -- `None` when `reason` is not a certificate-standing
    problem. `sign()` has two call sites that can report one of these three
    reasons (the final call, reached once a certificate was parsed; the
    earlier `info is None` refusal, reached when the adapter could not parse
    one at all -- unreachable with today's mock, but a real adapter at stage
    5.2 could report `certificate_invalid_at_signing` there too), and this is
    the one place both ask "does this reason count", so the two can never
    answer it differently (fix round 1 -- the same drift 3.7's own review
    caught: two call sites deciding the same thing independently)."""
    if reason in CERTIFICATE_STANDING_REASONS:
        return {"risk_indicator": "RI-05", "reason": reason}
    return None


async def get_certificate(db: AsyncSession, certificate_id: uuid.UUID) -> Certificate:
    cert = await repo.get_certificate(db, certificate_id)
    if cert is None:
        raise err("ERR-SYS-003")
    return cert


async def get_for_object(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID
) -> list[Signature]:
    return await repo.list_for_object(db, object_type=object_type, object_id=object_id)


async def required_purposes(db: AsyncSession, object_type: str) -> list[str]:
    """The ordered signature purposes `object_type` needs before it is
    complete -- read from `system_settings` via `settings_store`, never a
    literal in code (ruling 7): the Agency's still-unanswered question about
    exactly which roles must sign a permit is one admin-editable settings
    row, not an `if` in this module. `[]` for any `object_type` with no
    entry in `_REQUIREMENT_SETTINGS` -- not an error, since most object
    types (today: everything but "permit") have no signature requirement
    configured at all."""
    key = _REQUIREMENT_SETTINGS.get(object_type)
    if key is None:
        return []
    raw = await settings_store.get_str(db, key)
    return [purpose.strip() for purpose in raw.split(",") if purpose.strip()]


async def missing_purposes(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID
) -> list[str]:
    """`required_purposes(object_type)` minus whatever purpose already has a
    `valid` signature on `object_id`, in the configured order -- exactly what
    `ERR-SIGN-003` reports and what `require_complete` raises on. An
    `invalid` attempt is kept as evidence (ruling 8) and must never satisfy a
    requirement, so only `verification_status == "valid"` rows count."""
    required = await required_purposes(db, object_type)
    if not required:
        return []
    signed = await get_for_object(db, object_type=object_type, object_id=object_id)
    valid_purposes = {row.purpose for row in signed if row.verification_status == "valid"}
    return [purpose for purpose in required if purpose not in valid_purposes]


async def is_complete(db: AsyncSession, *, object_type: str, object_id: uuid.UUID) -> bool:
    """True once every required purpose has a valid signature -- and,
    equally, true for an object type nobody requires a signature on at all
    (`required_purposes` returns `[]`, so nothing can ever be missing).
    "Nothing required" and "nothing missing" are the same fact here, so this
    must always agree with `require_complete`, which reads the same list and
    never raises on that same empty case."""
    return not await missing_purposes(db, object_type=object_type, object_id=object_id)


async def carried_signatures_valid(
    db: AsyncSession, *, object_type: str, object_id: uuid.UUID
) -> bool:
    """Whether every signature this object ALREADY CARRIES is still recorded
    valid — a question about history, deliberately not about the requirement.

    `missing_purposes` above answers "is the CURRENT required set satisfied",
    which is the right question for a gate and the wrong one for a historical
    read: `permit_required_signatures` is admin-editable (ruling 7) and `tz/04`
    С11's «все три обязательны?» is still unanswered by the Agency, so the day
    that row grows a purpose, every object signed before it would start
    reporting its existing signatures as not valid. This answers instead: of
    the signatures actually taken, is any one of them now recorded invalid?

    Two row kinds are excluded, and both exclusions matter:

    * **An invalid `sign()` attempt is evidence, not a signature** (ruling 8) —
      a signatory who fat-fingers their ERI and retries leaves a stored invalid
      row beside their valid one, and that must not read as a broken document.
      Only rows that verified `valid` are carried.
    * **A `reverify` record is a verdict ON a row, never a row of its own.** It
      is written under `"{purpose}:reverify:{n}"` with `original_signature_id`
      in its record, so it is identified by that key rather than by parsing the
      purpose string. An invalid one DOWNGRADES the signature it names — which
      is the whole point: a certificate later found revoked makes this False
      while the object's own status is untouched. A reverify of an already
      invalid row names a row that was never carried, so it changes nothing.

    False for an object with no valid signature at all: fail-closed, and for the
    one caller (a permit's public page) unreachable, since a permit reaches
    `active` only once every required purpose has one.
    """
    rows = await get_for_object(db, object_type=object_type, object_id=object_id)
    downgraded = {
        row.verification.get("original_signature_id")
        for row in rows
        if row.verification_status == "invalid"
        and row.verification.get("original_signature_id") is not None
    }
    carried = [
        row
        for row in rows
        if row.verification_status == "valid"
        and row.verification.get("original_signature_id") is None
    ]
    return bool(carried) and not any(str(row.id) in downgraded for row in carried)


async def require_complete(db: AsyncSession, *, object_type: str, object_id: uuid.UUID) -> None:
    """3.11 calls exactly this before flipping a permit to ACTIVE (C11).
    Raises `ERR-SIGN-003` with `details.missing` naming every purpose still
    lacking a valid signature; silent whenever `missing_purposes` is empty,
    including the "nothing required for this object type" case -- by
    construction this can never disagree with `is_complete`, since both
    read the exact same list.

    Two limits this does NOT enforce, both left to 3.11 (fix wave), which
    owns who the signatories actually are:
    - it matches `purpose` STRINGS only, never the SIGNER's role against
      that purpose. `sign()` refuses a `purpose` outside
      `required_purposes(object_type)` when one is configured (below), but
      nothing here checks that the person who signed "permit_head" actually
      holds that role -- one user with one certificate can currently sign
      every required purpose on their own and this still reports complete.
    - it does not check that the valid signatures all cover the SAME
      `doc_hash` -- a permit regenerated after being partly signed still
      reads complete against signatures taken over the OLD bytes.
    """
    missing = await missing_purposes(db, object_type=object_type, object_id=object_id)
    if missing:
        raise err("ERR-SIGN-003", details={"missing": missing})


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
            # `begin_nested()` (a SAVEPOINT), not a bare call (fix wave —
            # mirrors `sign()`'s own insert race, see its except block for
            # the full reasoning): a bare `db.flush()` here, caught by
            # `except IntegrityError` with no savepoint, leaves the WHOLE
            # transaction aborted at the database level — so a stage-3.11
            # caller writing `try: sign() ... except DomainError:` would hit
            # `InFailedSQLTransaction` on its very next statement, where
            # every OTHER refusal in this module commits (or, here, simply
            # returns) cleanly. The SAVEPOINT scopes the rollback to just
            # this failed insert.
            async with db.begin_nested():
                cert = await repo.insert_certificate(db, info=info, user_id=None)
        except IntegrityError as exc:
            # Constraint-CHECKED, not constraint-blind (fix wave — the same
            # shape `sign()`'s own except already uses, and the same lesson:
            # IntegrityError IS a DBAPIError, and `exc.orig.__cause__`, not
            # `exc.orig`, is what actually names the constraint). Only
            # `uq_certificate_identity`'s own violation — two callers racing
            # to insert the SAME brand-new (serial_number, issuer) pair, the
            # exact shape create_contour's own number-taken race documents
            # (gis/service.py) — means "certificate_conflict". Anything
            # else on this SAME insert is a real defect, not this race:
            # `ck_certificates_validity_ordered` is reachable from a
            # client-crafted envelope with `valid_to < valid_from` and must
            # surface as itself, unmapped, never mislabeled a certificate
            # conflict. No audit call here (unlike sign()'s own
            # ERR-SIGN-002 paths, fix 1) — this is a benign insert race, not
            # a security event, and there is no document, purpose or object
            # to hang a `signatures` row on; the SAVEPOINT above already
            # undid the failed insert either way, so a bare `raise` here
            # leaves `db` exactly as it was before this whole `try` started.
            cause = exc.orig.__cause__ if exc.orig is not None else None
            if getattr(cause, "constraint_name", None) != "uq_certificate_identity":
                raise
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


def _raised_reason(
    verdict: Verdict, *, doc_hash: str, content_changed_reason: str | None
) -> str | None:
    """The reason `sign()` puts in the RAISED error's `details` — `verdict.reason`
    itself, unless the caller opted into a friendlier name for one specific,
    provable case.

    `"signature_invalid"` is the mock adapter's one ambiguous verdict:
    `eimzo.py::_verify_envelope` returns it both for an envelope it could not
    decode at all (a genuine forgery or corruption — `verdict.record["raw"]`
    stays `{}`, no `document_sha256` to compare) and for one that decoded
    fine, named a real certificate, and simply declares a `document_sha256`
    for bytes OTHER than the ones `sign()` just hashed into `doc_hash` — a
    signature that is honestly valid, over a package that has since changed.
    `raw["document_sha256"]` is only ever present in the second case, so its
    mere presence plus a mismatch against `doc_hash` IS the proof, not a
    guess: nothing here is inferred from the absence of evidence, only
    compared against evidence the envelope itself supplied. Every other
    reason (an unowned certificate, a missing purpose, a revoked
    certificate…) returns unchanged."""
    if content_changed_reason is None or verdict.reason != "signature_invalid":
        return verdict.reason
    original_hash = verdict.record.get("raw", {}).get("document_sha256")
    if original_hash is not None and original_hash != doc_hash:
        return content_changed_reason
    return verdict.reason


async def sign(
    db: AsyncSession,
    *,
    object_type: str,
    object_id: uuid.UUID,
    purpose: str,
    document: bytes,
    pkcs7: str,
    user: User,
    content_changed_reason: str | None = None,
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

    Order: if `object_type` has a configured requirement set at all
    (`required_purposes`) and `purpose` is not IN it, refuse immediately
    (fix wave — closes the sloppier half of `require_complete`'s own two
    documented limits: this stops a typo'd or made-up purpose from ever
    reaching storage, but it is still only a STRING check, not a check that
    THIS signer holds the role the purpose names — that stays 3.11's job)
    -> hash the bytes WE were handed (never trust the envelope's own claim
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

    **`content_changed_reason` (applications ruling 18, 2026-09-05) is an
    opt-in relabelling of the RAISED error only — it never touches what gets
    STORED.** `verdict.reason == "signature_invalid"` is the mock adapter's
    one genuinely ambiguous answer: `eimzo.py::_verify_envelope` returns it
    both for an envelope it could not decode at all (`raw` stays `{}`, no
    certificate, no claimed hash — a real forgery or corruption) and for one
    that decoded perfectly, named a real certificate, and simply carries a
    `document_sha256` for DIFFERENT bytes than the ones just hashed into
    `doc_hash` — a signature that is honestly valid over a package that has
    since changed. The two are told apart by that one fact: `raw` (copied
    verbatim into `verdict.record["raw"]`) exposes the ORIGINAL claimed hash,
    still there precisely because the envelope decoded — nothing is
    inferred, only compared. When a caller passes this reason AND that
    comparison finds a mismatch, the DomainError's `details.reason` is
    swapped to it; the stored `signatures.verification` row, its
    `verification_status`, and the audit entry's `basis` all keep the honest
    `"signature_invalid"` finding, exactly as before — evidence is never
    rewritten to read friendlier than what was actually found. Every OTHER
    reason (an unowned certificate, a missing purpose, a revoked
    certificate…) is untouched by this parameter, and every caller that
    leaves it `None` — every caller today except `applications`' `submit`
    and `_sign_decision` — gets today's exact behaviour, byte for byte:
    this is why the three permit-object `signature_invalid` tests in
    `tests/modules/signatures/` needed no change at all.
    """
    required = await required_purposes(db, object_type)
    if required and purpose not in required:
        # Fix wave: closes the sloppier half of `require_complete`'s own
        # documented gap — a purpose that could never satisfy ANY required
        # slot for this object type is refused before it reaches storage,
        # rather than being accepted and quietly never counting toward
        # completeness. Deliberately a STRING-membership check only: it does
        # NOT prove the signer holds the role `purpose` names (3.11's job —
        # see `require_complete`'s own docstring for the limit that remains).
        # `required == []` only for an object_type with NO entry in
        # `_REQUIREMENT_SETTINGS` at all — `settings_store.coerce` rejects
        # an empty-string override as malformed and falls back to the code
        # default, so an admin cannot reach `[]` this way — and `[]` is
        # treated as "no restriction", exactly matching `required_purposes`'s
        # own contract.
        await audit.log(
            db,
            action=SIGNATURE_CREATE,
            user_id=user.id,
            object_type=object_type,
            object_id=object_id,
            result="denied",
            basis="purpose_not_required",
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": "purpose_not_required"})

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
            extra=_ri05_extra(verdict.reason),
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
        # Ruling 10: mark a certificate-standing refusal for stage 4.2's risk
        # report (RI-05) -- additive only, same audit call, same commit.
        extra=_ri05_extra(verdict.reason),
    )

    if verdict.status == "invalid":
        # Early-commit pattern: the failed attempt IS the evidence (ruling 8)
        # and stage 4.2's risk reporting reads it — a raise before this commit
        # would roll the row and the audit entry back together with the very
        # exception they exist to explain. `_raised_reason` only ever changes
        # what THIS raise says, never `verification`/`basis` above, which is
        # already committed with the honest `verdict.reason`.
        await db.commit()
        raise err(
            "ERR-SIGN-001",
            details={
                "reason": _raised_reason(
                    verdict, doc_hash=doc_hash, content_changed_reason=content_changed_reason
                )
            },
        )

    return signature


# ---------------------------------------------------------------------------
# Task 7: the HTTP surface's own support — certificate list/bind/unbind, the
# object's signature list, and oversight re-verification. Everything above
# this line is Tasks 1-6; nothing above was changed to build this.
# ---------------------------------------------------------------------------


async def _holds_view_any(db: AsyncSession, user: User) -> bool:
    """Holds `signatures.view_any`, or is the superuser that passes every
    permission gate (decision #41 ruling 2) — the same two-branch shape
    `norms.service._holds_tariffs_publish` / `gis.service._may_manage_layers`
    / `admin.users_service._may_manage` use for a rule checked INSIDE a
    handler, as opposed to a `require_permission` dependency on the route
    itself. `GET /signatures` needs exactly this shape: the route also
    admits the object's OWN signer, who does not hold `view_any` at all, so
    the permission check cannot live in a route-level dependency the way
    `POST /signatures/{id}/reverify`'s `REVERIFY`-only gate does — that would
    reject the signer before `list_signatures_page` ever got a chance to
    check ownership instead (lesson: a permission check alone is not enough
    on a read path that also needs an ownership check)."""
    if await auth_repo.role_code(db, user) == SUPERUSER_ROLE:
        return True
    return VIEW_ANY in await auth_repo.permission_codes(db, user)


async def list_my_certificates(
    db: AsyncSession, *, user: User, params: PageParams
) -> tuple[list[Certificate], int]:
    """`GET /certificates`: the caller's own bound certificates only."""
    return await repo.list_certificates(
        db, user_id=user.id, offset=params.offset, limit=params.page_size
    )


async def register_certificate(db: AsyncSession, *, pkcs7: str, user: User) -> Certificate:
    """`POST /certificates` (ruling 4's second sentence): register a
    certificate ahead of any actual signing, from a self-contained signed
    challenge (`verify_attached` — there is no external document to hand
    alongside it, unlike `sign()`'s detached form).

    Reuses `bind_certificate`'s own ownership rule (`_ownership_reason`, fix
    rounds 1-2) via `bind_certificate` itself, but where THAT function leaves
    an unproven certificate quietly unbound for a later `sign()` call to
    explain (it has no document/purpose/object to hang the evidence on right
    there), this route has no such later call coming — an unproven
    presentation is refused directly, here, audited the same early-commit way
    as every other ERR-SIGN-001 refusal in this module (`bind_certificate`'s
    own docstring names this route explicitly as the reason its permissive
    branch cannot be the only check)."""
    adapter = get_eimzo_adapter()
    result = await adapter.verify_attached(pkcs7)
    info = result.subject_certificate
    if info is None or result.status_code != 1:
        reason = EIMZO_STATUS_REASONS.get(result.status_code, "signature_invalid")
        await audit.log(
            db,
            action=CERTIFICATE_BIND,
            user_id=user.id,
            object_type="certificate",
            result="denied",
            basis=reason,
            extra=_ri05_extra(reason),
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": reason})

    cert = await bind_certificate(db, info=info, user=user)  # may itself raise ERR-SIGN-001
    if cert.user_id != user.id:
        # bind_certificate's own "unproven, leave unbound" branch: re-derive
        # WHY, the same function `bind_certificate` itself would have asked,
        # so this refusal names the same reason `sign()` would have named had
        # there been a signature to attach it to.
        reason = await _ownership_reason(db, info=info, user=user)
        await audit.log(
            db,
            action=CERTIFICATE_BIND,
            user_id=user.id,
            object_type="certificate",
            object_id=cert.id,
            result="denied",
            basis=reason,
            extra=_ri05_extra(reason),
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": reason})

    await audit.log(
        db,
        action=CERTIFICATE_BIND,
        user_id=user.id,
        object_type="certificate",
        object_id=cert.id,
        result="success",
    )
    return cert


async def unbind_certificate(db: AsyncSession, *, certificate_id: uuid.UUID, user: User) -> None:
    """`DELETE /certificates/{id}`: the OWNER only. Sets `unbound_at`, never a
    delete and never a `status` change (pre-flight ruling P3: `status` is the
    certificate's own PKI state, per `design/02`, not our binding concept) —
    a certificate a signature references must survive forever (models.py).
    `get_certificate` raises `ERR-SYS-003` when the id does not exist at all;
    ownership is checked here, the same reason string `bind_certificate` uses
    for the identical fact reached from a different route."""
    cert = await get_certificate(db, certificate_id)
    if cert.user_id != user.id:
        await audit.log(
            db,
            action=CERTIFICATE_UNBIND,
            user_id=user.id,
            object_type="certificate",
            object_id=cert.id,
            result="denied",
            basis="certificate_owned_by_another_user",
        )
        await db.commit()
        raise err("ERR-SIGN-001", details={"reason": "certificate_owned_by_another_user"})
    cert.unbound_at = datetime.now(UTC)
    await db.flush()
    await audit.log(
        db,
        action=CERTIFICATE_UNBIND,
        user_id=user.id,
        object_type="certificate",
        object_id=cert.id,
        result="success",
    )


async def list_signatures_page(
    db: AsyncSession,
    *,
    object_type: str,
    object_id: uuid.UUID,
    user: User,
    params: PageParams,
) -> tuple[list[Signature], int]:
    """`GET /signatures`: the object's OWNER — defined, in a module that owns
    no `permits`/`applications` table of its own to ask (module docstring,
    Level 2), as anyone holding at least one VALID signature row against this
    object (fix wave — narrowed from "valid or invalid": once 3.9/3.11
    expose a real signing route, deliberately submitting a bad signature
    against a stranger's object would otherwise earn read access to its
    whole signature list; an invalid attempt is still evidence, ruling 8,
    but evidence is not ownership) — OR `signatures.view_any` (oversight).
    Checked here,
    inside the service, not as a route-level `require_permission` dependency,
    which would reject the object's own signer outright (see
    `_holds_view_any`'s own docstring). `ERR-ACL-001` on denial, matching the
    code `require_permission` itself raises for "no permission for this" —
    there is no territorial axis on `certificates`/`signatures` at all
    (`ERR-ACL-002` stays reserved for an actual zone mismatch elsewhere)."""
    if not await _holds_view_any(db, user):
        if not await repo.signed_by(
            db, object_type=object_type, object_id=object_id, signer_user_id=user.id
        ):
            raise err("ERR-ACL-001", details={"permission": VIEW_ANY})
    return await repo.list_for_object_page(
        db,
        object_type=object_type,
        object_id=object_id,
        offset=params.offset,
        limit=params.page_size,
    )


async def reverify(db: AsyncSession, *, signature_id: uuid.UUID, user: User) -> Signature:
    """`POST /signatures/{id}/reverify` (ruling 5): oversight's own re-check,
    writing a brand NEW row rather than touching the original — the stored
    verdict is evidence of what was true AT SIGNING TIME, not a cache to
    refresh in place.

    No crypto re-check is possible here: this module never stores the
    original document bytes, only `doc_hash` (module docstring) — the only
    fact a reverify can learn is the certificate's OWN current standing,
    fetched fresh via `_reconcile_status` the same way `sign()` does — which
    also means a reverify legitimately updates the CERTIFICATE row's own
    `status`/`revoked_at`, even though it must never touch the SIGNATURE row
    it was asked to re-check.

    Fix round 1 (Critical) — confirm or downgrade, NEVER upgrade: a check
    that can answer only one question ("is the certificate still good?")
    must never overturn the answer to a different one ("was the original
    signature itself valid?") that it has no way to re-examine.

    - `original.verification_status == "invalid"` -> stays `"invalid"`,
      carrying the ORIGINAL's own `reason` forward verbatim, regardless of
      what the certificate's live status says today — a broken signature,
      a stranger's PINFL, or no certificate parsed at all must never read
      as `"valid"` just because the certificate happens to be fine now.
    - originally `"valid"` and the certificate is now revoked/expired ->
      `"invalid"`, with the certificate-standing reason: a genuine
      downgrade, the one thing a reverify CAN discover.
    - originally `"valid"` and the certificate is still active -> stays
      `"valid"`, reason `None`: confirmed, not re-proven.

    `_ri05_extra(reason)` below (Task 5) reads whichever `reason` this
    lands on, so a downgrade caused by certificate standing is flagged the
    same way a `sign()`-time one is — no separate marking logic needed.

    The record itself says plainly what it re-checked
    (`"rechecked": "certificate_status"`) rather than copying the ORIGINAL
    verification blob wholesale, which would leave its `status_code` (a
    crypto-check result) sitting next to a reverify verdict, reading as
    though a cryptographic check had just been re-run. The original's own
    status/reason travel along for provenance under `original_*` keys,
    never bare ones a reader could mistake for this check's own.

    Fix round 1 (Important) — the ordinal suffix: `purpose` is written as
    `f"{original.purpose}:reverify:{n}"`, `n` the next ordinal among
    EXISTING reverify records for THIS original signature (matched by
    `original_signature_id` in their own stored record, start at 1).
    Per-ORIGINAL, not per-purpose: one object can legitimately hold an
    invalid AND a valid signature under the SAME purpose (ruling 8), and
    numbering by purpose alone would aim both signatures' re-verifications
    at the identical slot, blocking whichever is reverified second.
    Per-original numbering also means a genuine REPEAT reverify of the SAME
    signature (plan ruling 5: "evidence, not a cache" — oversight must be
    able to write a second dated "still valid" a year later) lands on its
    own fresh ordinal instead of colliding with the first — a sequential
    repeat is never refused.

    Fix round 2 — the race, restored: fix round 1 removed the SAVEPOINT +
    narrow `except IntegrityError` around this insert TOGETHER with the
    pre-check it had been bundled with, which was one correction too many —
    the pre-check (refusing a repeat outright) was wrong and stays gone,
    but the RACE it also happened to guard against is real and separate.
    Two `reverify()` calls for the SAME original, running concurrently,
    both read `existing` above before either commits, so both compute the
    identical `n` and the identical purpose string; when both verdicts land
    on `"valid"` (the common case — same certificate, same live status),
    the second flush loses a genuine collision on
    `uq_signatures_valid_purpose`. Guarded the same SAVEPOINT +
    narrow-`except` way `sign()` guards its own `ERR-SIGN-002` race, mapped
    to `ERR-SIGN-004` (409, already this module's code for a certificate-
    or-signature state conflict — reused, not a new code; `bind_certificate`
    (Task 4)'s own identity race is the SAME code for a different
    constraint, not the only thing this code means)."""
    original = await repo.get_signature(db, signature_id)
    if original is None:
        raise err("ERR-SYS-003")
    cert = await get_certificate(db, original.certificate_id)
    adapter = get_eimzo_adapter()
    live_status = await adapter.certificate_status(serial=cert.serial_number, issuer=cert.issuer)
    await _reconcile_status(db, cert, live_status)

    now = datetime.now(UTC)
    if original.verification_status == "invalid":
        new_status = "invalid"
        reason = original.verification.get("reason")
    elif live_status == "revoked":
        new_status, reason = "invalid", "certificate_revoked"
    elif live_status == "expired":
        new_status, reason = "invalid", "certificate_expired"
    else:
        new_status, reason = "valid", None

    record: dict[str, Any] = {
        "rechecked": "certificate_status",
        "original_signature_id": str(original.id),
        "original_verification_status": original.verification_status,
        "original_reason": original.verification.get("reason"),
        "certificate_status": live_status,
        "certificate_serial_number": cert.serial_number,
        "certificate_issuer": cert.issuer,
        "reverified_at": now.isoformat(),
        "reverified_by": str(user.id),
        "reason": reason,
    }

    # Per-original ordinal (fix round 1): count existing reverify rows that
    # already name THIS original in their own record — the same unpaged,
    # whole-object read `missing_purposes` uses, for the same reason (exact
    # membership matters more than pagination here).
    existing = await repo.list_for_object(
        db, object_type=original.object_type, object_id=original.object_id
    )
    already_reverified = sum(
        1 for row in existing if row.verification.get("original_signature_id") == str(original.id)
    )
    n = already_reverified + 1

    try:
        # `begin_nested()` (a SAVEPOINT), not a bare call — fix round 2,
        # restored after fix round 1 removed it TOGETHER with the pre-check
        # it had been bundled with (see the docstring above). This insert
        # can lose a genuine race between two concurrent reverify() calls
        # for the SAME original, and recovering from THAT failure still
        # needs to write an audit entry and commit on this SAME session
        # afterward — a bare `db.flush()` caught by `except IntegrityError`
        # with a plain `db.rollback()` would roll back the WHOLE
        # transaction, not just this insert's own failed statement (lesson
        # — `sign()`'s own identical SAVEPOINT carries the full reasoning).
        async with db.begin_nested():
            new_row = await repo.insert_signature(
                db,
                object_type=original.object_type,
                object_id=original.object_id,
                purpose=f"{original.purpose}:reverify:{n}",
                signer_user_id=original.signer_user_id,
                certificate_id=original.certificate_id,
                doc_hash=original.doc_hash,
                signature_value=original.signature_value,
                signed_at=original.signed_at,
                verification=record,
                verification_status=new_status,
            )
    except IntegrityError as exc:
        # IntegrityError IS a DBAPIError subclass — this narrow clause must
        # be checked first (lesson). Only `uq_signatures_valid_purpose`'s
        # own violation means "lost the ordinal race" — the `certificate_id`
        # FK and the `verification_status` CHECK are integrity violations on
        # this SAME insert too, and mapping either of THOSE to a conflict
        # would report an untrue reason for a real defect.
        # `exc.orig.__cause__` (not `exc.orig`) is what actually names the
        # constraint (lesson). Anything other than this ONE constraint
        # surfaces as itself, unmapped — the `begin_nested()` above already
        # undid the failed insert either way, so a bare `raise` leaves `db`
        # exactly as it was before this whole `try` started.
        cause = exc.orig.__cause__ if exc.orig is not None else None
        if getattr(cause, "constraint_name", None) != "uq_signatures_valid_purpose":
            raise
        # Two concurrent reverify() calls for the SAME original computed the
        # identical ordinal `n` and both verdicts landed on "valid" — a
        # genuine race, never a repeat being refused (plan ruling 5: a
        # repeat is legitimate and, run sequentially, succeeds on the next
        # ordinal without ever reaching this except).
        await audit.log(
            db,
            action=SIGNATURE_REVERIFY,
            user_id=user.id,
            object_type=original.object_type,
            object_id=original.object_id,
            result="denied",
            basis="concurrent_reverify",
        )
        await db.commit()
        raise err("ERR-SIGN-004", details={"reason": "concurrent_reverify"}) from exc

    await audit.log(
        db,
        action=SIGNATURE_REVERIFY,
        user_id=user.id,
        object_type=original.object_type,
        object_id=original.object_id,
        result="success",
        basis=reason,
        extra=_ri05_extra(reason),
    )
    return new_row
