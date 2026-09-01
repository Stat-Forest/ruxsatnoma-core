import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.time import business_today
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Applicant, Representation, User
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import repo, service
from tests.modules.auth.test_sessions import make_user

DOC = b"the-permit-bytes"
OBJ = uuid.uuid4()


def _pkcs7(pinfl: str, serial: str = "SER-1") -> str:
    return encode_mock_signature(document=DOC, serial=serial, issuer="ISS-1", pinfl=pinfl)


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call — `users.pinfl` is
    UNIQUE, and fixtures in this file may run many times against the same
    shared test database."""
    return f"{secrets.randbelow(10**14):014d}"


def _stir() -> str:
    """A fresh, valid-shape (`^[0-9]{9}$`) stir per call — `applicants.stir`
    is UNIQUE, same reasoning as `_pinfl()`. 9 digits is also exactly what
    `_owns_certificate` uses to recognise an organisation certificate."""
    return f"{secrets.randbelow(10**9):09d}"


async def _denied_audit_count(db: AsyncSession, *, object_id: uuid.UUID) -> int:
    """How many `result="denied"` `signature.create` entries exist for one
    object. `audit_log` is append-only (RI-06) and shared across the whole
    suite's runs, so an EXACT count is only ever safe against an `object_id`
    no other test could produce — every fix-round-3 test below uses a fresh
    `uuid.uuid4()` for exactly this reason, never the shared `OBJ`."""
    return (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.object_type == "permit",
                AuditLog.object_id == object_id,
                AuditLog.action == service.SIGNATURE_CREATE,
                AuditLog.result == "denied",
            )
        )
    ).scalar_one()


@pytest.fixture
async def a_user(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.fixture
async def another_user(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.fixture(autouse=True)
async def _reset_shared_certificate_identity(db: AsyncSession) -> AsyncIterator[None]:
    """`_pkcs7()`'s identity (SER-1/ISS-1) is fixed and reused by every test
    below, including `test_a_certificate_belonging_to_another_user_is_refused`,
    which exercises `bind_certificate`'s early-commit refusal path — unlike a
    normal test, that commit really does persist to the shared, persistent
    test database (`.claude/lessons.md`: "the test database is shared and
    persistent"; "a negative test's failure path can leave state that poisons
    a different test's invariant"). Clear any row tied to this identity both
    before and after each test, so this file's outcome depends on neither run
    order nor a previous run's leftovers. Scoped to this one identity, never a
    blanket `DELETE FROM certificates` (same lesson)."""

    async def _clear() -> None:
        await db.execute(
            text(
                "DELETE FROM signatures WHERE certificate_id IN "
                "(SELECT id FROM certificates WHERE serial_number = :sn AND issuer = :iss)"
            ),
            {"sn": "SER-1", "iss": "ISS-1"},
        )
        await db.execute(
            text("DELETE FROM certificates WHERE serial_number = :sn AND issuer = :iss"),
            {"sn": "SER-1", "iss": "ISS-1"},
        )
        await db.commit()

    await _clear()
    yield
    await _clear()


@pytest.mark.asyncio
async def test_signing_stores_the_row_and_binds_the_certificate(db, a_user):
    row = await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    assert row.verification_status == "valid"
    assert row.doc_hash == __import__("hashlib").sha256(DOC).hexdigest()
    cert = await service.get_certificate(db, row.certificate_id)
    assert cert.user_id == a_user.id  # bound on first use (ruling 4)


@pytest.mark.asyncio
async def test_a_second_signature_for_the_same_purpose_is_a_conflict(db, a_user):
    kw: dict[str, Any] = dict(
        object_type="permit", object_id=OBJ, purpose="permit_head", document=DOC
    )
    await service.sign(db, **kw, pkcs7=_pkcs7(a_user.pinfl), user=a_user)
    with pytest.raises(DomainError) as exc:
        await service.sign(db, **kw, pkcs7=_pkcs7(a_user.pinfl), user=a_user)
    assert exc.value.code == "ERR-SIGN-002"


@pytest.mark.asyncio
async def test_a_certificate_belonging_to_another_user_is_refused(db, a_user, another_user):
    await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=uuid.uuid4(),
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(a_user.pinfl),
            user=another_user,
        )
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_owned_by_another_user"


@pytest.mark.asyncio
async def test_a_signature_over_other_bytes_is_refused_and_still_recorded(db, a_user):
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=OBJ,
            purpose="permit_head",
            document=b"DIFFERENT",
            pkcs7=_pkcs7(a_user.pinfl),
            user=a_user,
        )
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "signature_invalid"
    # the failed attempt is evidence and is kept (ruling 8)
    rows = await service.get_for_object(db, object_type="permit", object_id=OBJ)
    assert [r.verification_status for r in rows] == ["invalid"]


@pytest.mark.asyncio
async def test_a_certificate_whose_pinfl_differs_from_the_caller_is_refused(db, a_user):
    """Fix round 1, ruling 1: ownership must be proven before a first bind,
    not just DB `user_id`. A stranger's PINFL embedded in the envelope is
    refused even though nothing yet in the DB says the certificate belongs
    to anyone else."""
    stranger_pinfl = _pinfl()
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=OBJ,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stranger_pinfl),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_pinfl_mismatch"
    # the attempt is still evidence, same as any other invalid verdict
    # (ruling 8) — the certificate itself is recorded but stays unbound.
    rows = await service.get_for_object(db, object_type="permit", object_id=OBJ)
    assert [r.verification_status for r in rows] == ["invalid"]
    cert = await service.get_certificate(db, rows[0].certificate_id)
    assert cert.user_id is None


@pytest.mark.asyncio
async def test_a_certificate_whose_pinfl_matches_the_caller_binds_normally(db, a_user):
    """The other half of ruling 1: the common case — the certificate's own
    PINFL is the caller's own — must keep working exactly as before."""
    row = await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    assert row.verification_status == "valid"
    cert = await service.get_certificate(db, row.certificate_id)
    assert cert.user_id == a_user.id


@pytest.mark.asyncio
async def test_signing_with_a_previously_unbound_own_certificate_rebinds_it(db, a_user):
    """Fix round 1, ruling 2: unbinding is a cabinet convenience ("stop
    listing this key"), not a revocation — signing again with a key the
    caller had unbound from their own list simply re-lists it."""
    first = await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    cert = await service.get_certificate(db, first.certificate_id)
    cert.unbound_at = datetime.now(UTC)
    await db.flush()

    await service.sign(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    cert_again = await service.get_certificate(db, first.certificate_id)
    assert cert_again.unbound_at is None


@pytest.mark.asyncio
async def test_a_caller_with_an_effective_representation_signs_with_the_org_certificate(db, a_user):
    """Fix round 2: the unblocked organisation-certificate half of ownership
    proof. `_pkcs7`'s `pinfl` argument doubles as `pinfl_or_stir` — a 9-digit
    value makes `_owns_certificate` read it as an organisation STIR and ask
    `auth.service.has_effective_representation` instead of matching PINFL."""
    stir = _stir()
    applicant = Applicant(kind="legal", stir=stir, name="OOO Represented")
    db.add(applicant)
    await db.flush()
    db.add(
        Representation(
            applicant_id=applicant.id,
            user_id=a_user.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()

    row = await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(stir),
        user=a_user,
    )
    assert row.verification_status == "valid"
    cert = await service.get_certificate(db, row.certificate_id)
    assert cert.user_id == a_user.id  # bound on first use, same as personal PINFL


@pytest.mark.asyncio
async def test_a_caller_with_no_representation_for_the_org_stir_is_refused(db, a_user):
    """The negative half: a STIR nobody has ever represented `a_user` for is
    refused exactly like a stranger's personal PINFL — same evidence-then-
    raise path, same reason, and the certificate stays unbound."""
    stir = _stir()  # no Applicant/Representation row at all for this STIR
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=OBJ,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stir),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_pinfl_mismatch"
    rows = await service.get_for_object(db, object_type="permit", object_id=OBJ)
    assert [r.verification_status for r in rows] == ["invalid"]
    cert = await service.get_certificate(db, rows[0].certificate_id)
    assert cert.user_id is None


@pytest.mark.asyncio
async def test_a_caller_whose_representation_has_expired_is_refused(db, a_user):
    """`business_today()` is what makes this testable (per its own module
    docstring): `valid_until` in the past relative to it, `status` still
    'active' (the 3.4 daily expiry job has not run yet) — the read-time
    effectiveness check itself must catch this, not rely on the job."""
    stir = _stir()
    applicant = Applicant(kind="legal", stir=stir, name="OOO Expired")
    db.add(applicant)
    await db.flush()
    db.add(
        Representation(
            applicant_id=applicant.id,
            user_id=a_user.id,
            basis="org_eri",
            valid_from=business_today() - timedelta(days=30),
            valid_until=business_today() - timedelta(days=1),
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=OBJ,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stir),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_pinfl_mismatch"


# --- Fix round 3 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_signature_is_audited_but_adds_no_extra_row(db, a_user):
    """Fix round 3, fix 1: a duplicate-signature refusal must leave a trace —
    an audit entry, but no second `signatures` row. A duplicate is a
    double-click far more often than an attack, and a row per retry would
    fill the evidence table with noise stage 4.2 then has to filter back
    out; the audit entry alone answers "who tried and when"."""
    own_obj = uuid.uuid4()
    await service.sign(
        db,
        object_type="permit",
        object_id=own_obj,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=own_obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(a_user.pinfl),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-002"
    rows = await service.get_for_object(db, object_type="permit", object_id=own_obj)
    assert [r.verification_status for r in rows] == ["valid"]  # no second row
    assert await _denied_audit_count(db, object_id=own_obj) == 1


@pytest.mark.asyncio
async def test_a_racing_duplicate_signature_is_also_audited(db, a_user, monkeypatch):
    """The service-level pre-check above is the common path to
    `ERR-SIGN-002`; this pins the OTHER one — two concurrent `sign()` calls
    both pass the pre-check, and the partial unique index itself catches the
    second INSERT. Stood in for the same way `test_contours_api.py`'s own
    `version_conflict` test stands in for its race (`gis/service.py`):
    monkeypatch the pre-check stale, so the real INSERT is what raises."""
    own_obj = uuid.uuid4()
    await service.sign(
        db,
        object_type="permit",
        object_id=own_obj,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )

    async def _stale_no_valid_signature(db, object_type, object_id, purpose):
        return None  # stands in for the race: the other call already committed

    monkeypatch.setattr(repo, "get_valid_signature", _stale_no_valid_signature)

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=own_obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(a_user.pinfl),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-002"
    rows = await service.get_for_object(db, object_type="permit", object_id=own_obj)
    assert [r.verification_status for r in rows] == ["valid"]  # no second row
    assert await _denied_audit_count(db, object_id=own_obj) == 1


@pytest.mark.asyncio
async def test_a_caller_whose_representation_expires_after_binding_is_refused_next_time(db, a_user):
    """Fix round 3, fix 2: ownership is re-proven on EVERY `sign()` call, not
    only trusted once from the stored `certificates.user_id` — the
    `user_id == user.id` fast path used to skip `_owns_certificate` entirely,
    so a representation that expired or was revoked AFTER the certificate
    was bound kept authorising signatures forever. Signs once while the
    representation is active, expires it, then signs again with the SAME
    already-bound certificate and asserts the second attempt is refused."""
    stir = _stir()
    applicant = Applicant(kind="legal", stir=stir, name="OOO Time-Boxed")
    db.add(applicant)
    await db.flush()
    representation = Representation(
        applicant_id=applicant.id,
        user_id=a_user.id,
        basis="org_eri",
        valid_from=business_today() - timedelta(days=10),
        valid_until=business_today() + timedelta(days=10),
    )
    db.add(representation)
    await db.flush()

    first = await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(stir),
        user=a_user,
    )
    assert first.verification_status == "valid"
    cert = await service.get_certificate(db, first.certificate_id)
    assert cert.user_id == a_user.id  # bound on the first, still-effective sign

    representation.valid_until = business_today() - timedelta(days=1)
    await db.flush()

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=uuid.uuid4(),
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stir),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_pinfl_mismatch"
    # still the SAME gate as a stranger's certificate — never "owned by
    # another user": the certificate stays bound to a_user throughout, this
    # is the re-proof gate refusing a NEW signature, not an ownership change.
    cert_after = await service.get_certificate(db, first.certificate_id)
    assert cert_after.user_id == a_user.id


@pytest.mark.asyncio
async def test_a_signer_with_no_recorded_pinfl_gets_an_honest_reason(db):
    """Fix round 3, fix 3: a staff user created with `pinfl=None` (real —
    `users.pinfl` is nullable, e.g. a leshoz head whose PINFL was never
    recorded) is not lying with a wrong certificate; the missing datum is on
    our side. `certificate_pinfl_mismatch` would say the opposite of what
    actually happened, so this case gets its own honest reason."""
    user_without_pinfl = await make_user(db, pinfl=None)
    own_obj = uuid.uuid4()
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=own_obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(_pinfl()),  # any personal certificate at all
            user=user_without_pinfl,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "signer_pinfl_unknown"
    rows = await service.get_for_object(db, object_type="permit", object_id=own_obj)
    assert [r.verification_status for r in rows] == ["invalid"]


@pytest.mark.asyncio
async def test_a_racing_certificate_presentation_is_a_conflict_not_a_500(db, a_user, monkeypatch):
    """Fix round 3, fix 5: two callers presenting the same brand-new
    `(serial_number, issuer)` at once both pass `get_certificate_by_identity`
    before either commits its own INSERT — `repo.insert_certificate`'s flush
    then raises `IntegrityError` on `uq_certificate_identity` for the second
    one. Mapped to a domain error the same way `create_contour`'s own
    `number_taken` race is (`gis/service.py`), never an uncaught 500. Stood
    in the same way that test does: monkeypatch the read stale."""
    await service.sign(
        db,
        object_type="permit",
        object_id=OBJ,
        purpose="permit_head",
        document=DOC,
        pkcs7=_pkcs7(a_user.pinfl),
        user=a_user,
    )

    async def _stale_not_found(db, serial_number, issuer):
        return None  # stands in for the race: the other call already flushed its row

    monkeypatch.setattr(repo, "get_certificate_by_identity", _stale_not_found)

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=uuid.uuid4(),
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(a_user.pinfl),
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-004"
    assert exc.value.details == {"reason": "certificate_conflict"}
    # The losing flush left the transaction aborted at the database level
    # (Postgres, not just SQLAlchemy) — clear it before this file's autouse
    # cleanup fixture reuses the same session in its own teardown.
    await db.rollback()


@pytest.mark.asyncio
async def test_an_unrelated_integrity_violation_is_not_mislabeled_already_signed(
    db, a_user, monkeypatch
):
    """Fix round 3, fix 6: only `uq_signatures_valid_purpose`'s own violation
    means "already signed" — anything else on this same INSERT must surface
    as itself. Forces a DIFFERENT constraint (the `certificate_id` FK) by
    substituting a certificate id that does not exist, and asserts the raw
    `IntegrityError` propagates unmapped, never becoming `ERR-SIGN-002`."""
    real_insert_signature = repo.insert_signature

    async def _insert_with_a_nonexistent_certificate(db, **kwargs):
        kwargs["certificate_id"] = uuid.uuid4()
        return await real_insert_signature(db, **kwargs)

    monkeypatch.setattr(repo, "insert_signature", _insert_with_a_nonexistent_certificate)

    with pytest.raises(IntegrityError):
        await service.sign(
            db,
            object_type="permit",
            object_id=uuid.uuid4(),
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(a_user.pinfl),
            user=a_user,
        )
    # The unmapped violation left the transaction aborted at the database
    # level — clear it before this file's autouse cleanup fixture reuses the
    # same session in its own teardown.
    await db.rollback()
