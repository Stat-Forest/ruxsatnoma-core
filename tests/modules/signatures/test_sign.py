import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
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
