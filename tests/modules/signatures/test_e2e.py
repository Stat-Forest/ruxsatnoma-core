"""End-to-end: C11, the permit's 3+1 — played out entirely through the
service, because `permits` does not exist yet (3.11 will drive this same
sequence through HTTP). Four different users of one organisation each sign
the identical bytes with their own certificate, one per required purpose
(`test_requirements.py::test_the_default_permit_requirement_is_the_three_
plus_one`); the object becomes complete only once every purpose holds a
valid signature, and neither a repeat of an already-satisfied purpose nor a
revoked certificate may ever count toward it (ruling 8, RI-05)."""

import secrets
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.time import business_today
from app.modules.auth.models import Applicant, Representation, User
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
from tests.modules.auth.test_sessions import make_user

DOC = b"the-permit-bytes"

# `test_the_default_permit_requirement_is_the_three_plus_one`'s own order —
# asserted again below so this test fails loudly, not silently, if the two
# ever drift apart.
# Ruling #210: the recipient line is no longer required — three leshoz lines.
THE_THREE_PURPOSES = [
    "permit_head",
    "permit_chief_forester",
    "permit_accountant",
]


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call — `users.pinfl` is
    UNIQUE and this file's fixtures may run many times against the shared,
    persistent test database (mirrors `test_sign.py`'s own helper)."""
    return f"{secrets.randbelow(10**14):014d}"


def _stir() -> str:
    """A fresh, valid-shape (`^[0-9]{9}$`) stir per call — `applicants.stir`
    is UNIQUE, same reasoning as `_pinfl()`."""
    return f"{secrets.randbelow(10**9):09d}"


def _pkcs7(stir: str, *, serial: str) -> str:
    """Every signer below presents the SAME organisation STIR (one
    organisation, C11's own scenario) but a DISTINCT certificate identity —
    a certificate binds to exactly one user, so four signers need four
    `(serial, issuer)` pairs even though they share one `pinfl_or_stir`."""
    return encode_mock_signature(document=DOC, serial=serial, issuer=f"ISS-{serial}", pinfl=stir)


@pytest.fixture
async def one_organisation(db: AsyncSession) -> Applicant:
    """One legal-entity `Applicant` — the "of one organisation" in C11's own
    scenario description — that this test's four signers will each hold
    their own effective representation for."""
    applicant = Applicant(kind="legal", stir=_stir(), name="Test Leshoz Org")
    db.add(applicant)
    await db.flush()
    return applicant


async def _staff_member(db: AsyncSession, *, applicant: Applicant) -> User:
    """A fresh user holding their own effective representation for
    `applicant` — one organisation, several people each authorised to
    represent it, the same shape `test_sign.py`'s own org-certificate tests
    use for a single signer."""
    user = await make_user(db, pinfl=_pinfl())
    db.add(
        Representation(
            applicant_id=applicant.id,
            user_id=user.id,
            basis="org_eri",
            valid_from=business_today(),
        )
    )
    await db.flush()
    return user


async def test_c11_the_permits_three_lines(db: AsyncSession, one_organisation: Applicant):
    assert await service.required_purposes(db, "permit") == THE_THREE_PURPOSES
    obj = uuid.uuid4()
    assert one_organisation.stir is not None  # narrows Applicant.stir's nullable column type
    stir = one_organisation.stir
    # `certificates.serial_number`/`issuer` are UNIQUE and certificates are
    # never deleted, so a fixed literal like "SER-C11-0" binds to whichever
    # run's signer got there first and refuses every later run with
    # `certificate_owned_by_another_user` (`.claude/lessons.md`: "the test
    # database is shared, persistent, and never empty") — randomise per run.
    run = uuid.uuid4().hex[:10]

    # Nothing is bound in advance (brief): each signer's certificate is
    # created here, on first use, by `sign()` itself via `bind_certificate`.
    for index, purpose in enumerate(THE_THREE_PURPOSES):
        signer = await _staff_member(db, applicant=one_organisation)
        row = await service.sign(
            db,
            object_type="permit",
            object_id=obj,
            purpose=purpose,
            document=DOC,
            pkcs7=_pkcs7(stir, serial=f"SER-{run}-{index}"),
            user=signer,
        )
        assert row.verification_status == "valid"

        is_last = index == len(THE_THREE_PURPOSES) - 1
        assert await service.is_complete(db, object_type="permit", object_id=obj) is is_last

    # 3.11 calls exactly this before flipping a permit to ACTIVE — silent
    # once every purpose is satisfied.
    await service.require_complete(db, object_type="permit", object_id=obj)
    assert await service.missing_purposes(db, object_type="permit", object_id=obj) == []

    # A fifth attempt at an already-satisfied purpose is a conflict, not a
    # silent extra signature — `uq_signatures_valid_purpose` (ERR-SIGN-002),
    # not a fifth required slot.
    fifth_signer = await _staff_member(db, applicant=one_organisation)
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stir, serial=f"SER-{run}-4"),
            user=fifth_signer,
        )
    assert exc.value.code == "ERR-SIGN-002"
    assert await service.is_complete(db, object_type="permit", object_id=obj) is True

    # A signature by a user whose certificate is revoked is refused for
    # RI-05, not for the duplicate-purpose conflict above (`build_verdict`
    # checks certificate standing before `sign()` ever reaches the
    # already-signed check) — and completeness is unaffected either way, an
    # invalid attempt can never occupy a purpose's slot (ruling 8).
    revoked_signer = await _staff_member(db, applicant=one_organisation)
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj,
            purpose="permit_head",
            document=DOC,
            pkcs7=_pkcs7(stir, serial=f"REVOKED-{run}-5"),
            user=revoked_signer,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_revoked"
    assert await service.is_complete(db, object_type="permit", object_id=obj) is True
