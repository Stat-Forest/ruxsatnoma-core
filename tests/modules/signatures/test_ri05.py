"""RI-05 (docs/tz/10-klassifikatory.md): an attempt to sign with a
certificate that is not in good standing -- revoked, expired, or not within
its own validity window at the moment it signed. Task 5 only marks the audit
entry for these three reasons on `service.sign`'s final `audit.log` call
(plan ruling 10); stage 4.2, not built yet, is what later reads the marker
and turns it into a risk report.

Certificate identities below use a random suffix rather than a fixed literal
like the brief's own "REVOKED-1": `certificates.serial_number`/`issuer` are
unique, and this test database is shared and persistent across runs
(`.claude/lessons.md`: "the test database is shared, persistent..." and "a
negative test's failure path can leave state that poisons a different
test's invariant"). A fixed identity, once bound to one run's `a_user`, would
make a LATER run refuse with `certificate_owned_by_another_user` instead of
ever exercising the certificate-standing path the test means to check.
`object_id` is likewise fresh per test and used to scope the audit-log
lookup, rather than an unscoped "most recent row in the whole table" query,
for the same shared-database reason (mirrors `test_sign.py`'s own
`_denied_audit_count`)."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
from tests.modules.auth.test_sessions import make_user

DOC = b"doc"


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call -- `users.pinfl` is
    UNIQUE and this file's fixtures may run many times against the shared
    test database (mirrors `test_sign.py`'s own helper)."""
    return f"{secrets.randbelow(10**14):014d}"


@pytest.fixture
async def a_user(db: AsyncSession) -> User:
    """Not shared `conftest.py` (ruling P4: later tasks add their own
    fixtures rather than growing that file) -- `a_user`/`another_user` don't
    exist there (only `a_certificate` does), so this is the same local
    pattern Task 4 already used in `test_sign.py`."""
    return await make_user(db, pinfl=_pinfl())


async def _latest_signature_create_entry(db: AsyncSession, *, object_id: uuid.UUID) -> AuditLog:
    """The one `signature.create` entry for THIS test's own `object_id` --
    scoped, never the whole table's most-recent row (shared DB, see module
    docstring). Ordered by `(occurred_at, id)` per `AuditLog`'s own docstring
    (`occurred_at` is transaction-start time and does not by itself
    total-order rows written in the same transaction; `id` is a monotonic
    UUIDv7) -- moot for a single row, kept for the same reason the model
    documents it: correct even if a scenario here ever writes more than one."""
    entry = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.action == service.SIGNATURE_CREATE,
                    AuditLog.object_id == object_id,
                )
                .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None
    return entry


@pytest.mark.asyncio
async def test_a_revoked_certificate_is_refused_and_flagged_as_ri05(db: AsyncSession, a_user: User):
    assert a_user.pinfl is not None  # narrows User.pinfl's nullable column type
    obj_id = uuid.uuid4()
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"REVOKED-{uuid.uuid4().hex[:10]}",
        issuer="ISS-1",
        pinfl=a_user.pinfl,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="permit_head",
            document=DOC,
            pkcs7=pkcs7,
            user=a_user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_revoked"

    entry = await _latest_signature_create_entry(db, object_id=obj_id)
    assert entry.result == "denied"
    assert entry.extra is not None
    assert entry.extra["risk_indicator"] == "RI-05"
    assert entry.extra["reason"] == "certificate_revoked"


@pytest.mark.asyncio
async def test_an_expired_certificate_is_refused_and_flagged_as_ri05(
    db: AsyncSession, a_user: User
):
    assert a_user.pinfl is not None  # narrows User.pinfl's nullable column type
    obj_id = uuid.uuid4()
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"EXPIRED-{uuid.uuid4().hex[:10]}",
        issuer="ISS-1",
        pinfl=a_user.pinfl,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="permit_head",
            document=DOC,
            pkcs7=pkcs7,
            user=a_user,
        )
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_expired"

    entry = await _latest_signature_create_entry(db, object_id=obj_id)
    assert entry.result == "denied"
    assert entry.extra is not None
    assert entry.extra["risk_indicator"] == "RI-05"
    assert entry.extra["reason"] == "certificate_expired"


@pytest.mark.asyncio
async def test_a_certificate_invalid_at_signing_time_is_flagged_as_ri05(
    db: AsyncSession, a_user: User
):
    """Live status stays `active` (never revoked/expired) -- refused only
    because the trusted `signed_at` falls outside the certificate's own
    `[valid_from, valid_to]` window (plan ruling 5: compared against
    `signed_at`, never `now`)."""
    assert a_user.pinfl is not None  # narrows User.pinfl's nullable column type
    obj_id = uuid.uuid4()
    now = datetime.now(UTC)
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"SER-{uuid.uuid4().hex[:10]}",
        issuer="ISS-1",
        pinfl=a_user.pinfl,
        valid_from=now - timedelta(days=10),
        valid_to=now - timedelta(days=1),
        signed_at=now,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="permit_head",
            document=DOC,
            pkcs7=pkcs7,
            user=a_user,
        )
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_invalid_at_signing"

    entry = await _latest_signature_create_entry(db, object_id=obj_id)
    assert entry.result == "denied"
    assert entry.extra is not None
    assert entry.extra["risk_indicator"] == "RI-05"
    assert entry.extra["reason"] == "certificate_invalid_at_signing"


@pytest.mark.asyncio
async def test_an_ownership_refusal_is_not_flagged_as_ri05(db: AsyncSession, a_user: User):
    """`certificate_pinfl_mismatch` is an ownership problem, not a
    certificate-standing one (Task 5 brief) -- must NOT be marked RI-05, or
    stage 4.2's risk report would drown in events that were never about a
    bad certificate."""
    obj_id = uuid.uuid4()
    stranger_pinfl = _pinfl()
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"SER-{uuid.uuid4().hex[:10]}",
        issuer="ISS-1",
        pinfl=stranger_pinfl,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="permit_head",
            document=DOC,
            pkcs7=pkcs7,
            user=a_user,
        )
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "certificate_pinfl_mismatch"

    entry = await _latest_signature_create_entry(db, object_id=obj_id)
    assert entry.result == "denied"
    assert entry.extra is None
