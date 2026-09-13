"""`sign_simple` (ruling #183, docs/decisions.md; stage 10, track B1): a
citizen acting for THEMSELVES signs a document with a button, no ERI
certificate at all — an applicant session exists only through OneID or
E-IMZO login (decision #32, no password), so the signer is already known by
PINFL. `sign_simple` itself takes no position on WHETHER a simple signature
is allowed for an object — `tests/modules/permits/test_simple_signature.py`
is where the CALLER's rule (the application's `on_behalf`) is exercised end
to end. This file is the module's own, object-type-agnostic surface: the
purpose/PINFL/duplicate guards, the stored row's shape, the `GET /signatures`
`kind` filter and `reverify`'s no-op on a `kind='simple'` row.

Every `object_id` below is a fresh `uuid.uuid4()` per test — this test
database is shared and persistent (lesson), and `uq_signatures_valid_purpose`
would otherwise let one test's leftover row refuse another's first attempt.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.core.schemas import PageParams
from app.modules.audit.models import AuditLog
from app.modules.integrations.adapters.eimzo import encode_mock_signature
from app.modules.signatures import service
from app.modules.signatures.models import Signature
from tests.modules.auth.test_sessions import make_user

DOC = b"the-permit-bytes"
# In the default `permit_required_signatures`; ruling #210 dropped the recipient line.
PURPOSE = "permit_accountant"


def _pinfl() -> str:
    """A fresh, valid-shape (`^[0-9]{14}$`) pinfl per call — `users.pinfl` is
    UNIQUE and this test DB is shared and persistent (mirrors `test_sign.py`'s
    own helper)."""
    return f"{secrets.randbelow(10**14):014d}"


async def _denied_count(db, *, object_id: uuid.UUID, action: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.object_type == "permit",
                AuditLog.object_id == object_id,
                AuditLog.action == action,
                AuditLog.result == "denied",
            )
        )
    ).scalar_one()


async def test_sign_simple_stores_a_valid_row_with_no_certificate(db):
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()

    row = await service.sign_simple(
        db,
        object_type="permit",
        object_id=obj_id,
        purpose=PURPOSE,
        document=DOC,
        user=user,
        ip="203.0.113.7",
    )
    await db.commit()

    assert row.kind == "simple"
    assert row.certificate_id is None
    assert row.signature_value == ""
    assert row.signer_user_id == user.id
    assert row.verification_status == "valid"
    assert row.doc_hash == hashlib.sha256(DOC).hexdigest()
    assert row.verification == {
        "kind": "simple",
        "pinfl": user.pinfl,
        "auth_method": "eimzo",  # no `oneid_profile` was ever set on this test user
        "ip": "203.0.113.7",
    }

    entry = (
        await db.scalars(
            select(AuditLog).where(
                AuditLog.object_type == "permit",
                AuditLog.object_id == obj_id,
                AuditLog.action == service.SIGNATURE_CREATE_SIMPLE,
            )
        )
    ).one()
    assert entry.result == "success"
    assert entry.user_id == user.id


async def test_sign_simple_reports_oneid_as_the_auth_method_when_the_profile_exists(db):
    """`auth_method` reads `user.oneid_profile`, the exact field `auth.
    service.register_applicant` already reads for its own `verify_source` —
    reused, not a second convention."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    user.oneid_profile = {"pinfl": user.pinfl}
    await db.flush()

    row = await service.sign_simple(
        db,
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose=PURPOSE,
        document=DOC,
        user=user,
    )
    await db.commit()
    assert row.verification["auth_method"] == "oneid"
    assert row.verification["ip"] is None


async def test_sign_simple_refuses_an_unrequired_purpose(db):
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()

    with pytest.raises(DomainError) as exc:
        await service.sign_simple(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="not_a_real_purpose",
            document=DOC,
            user=user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details == {"reason": "purpose_not_required"}

    assert await _denied_count(db, object_id=obj_id, action=service.SIGNATURE_CREATE_SIMPLE) == 1
    rows = await service.get_for_object(db, object_type="permit", object_id=obj_id)
    assert rows == [], "a refused attempt writes no signature row of its own"


async def test_sign_simple_refuses_an_unknown_signer_pinfl(db):
    """The signer's PINFL comes from `user.pinfl` — the SAME field
    `_ownership_reason` reads for a personal certificate, never invented here
    as a second lookup. `make_user` with no `pinfl=` override leaves it NULL,
    the real shape of a staff account created before OneID/E-IMZO ever ran."""
    user = await make_user(db, role_code="applicant")
    assert user.pinfl is None
    obj_id = uuid.uuid4()

    with pytest.raises(DomainError) as exc:
        await service.sign_simple(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose=PURPOSE,
            document=DOC,
            user=user,
        )
    assert exc.value.code == "ERR-SIGN-001"
    assert exc.value.details == {"reason": "signer_pinfl_unknown"}

    assert await _denied_count(db, object_id=obj_id, action=service.SIGNATURE_CREATE_SIMPLE) == 1


async def test_sign_simple_refuses_a_duplicate_purpose(db):
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()

    await service.sign_simple(
        db, object_type="permit", object_id=obj_id, purpose=PURPOSE, document=DOC, user=user
    )
    await db.commit()

    with pytest.raises(DomainError) as exc:
        await service.sign_simple(
            db, object_type="permit", object_id=obj_id, purpose=PURPOSE, document=DOC, user=user
        )
    assert exc.value.code == "ERR-SIGN-002"

    rows = await service.get_for_object(db, object_type="permit", object_id=obj_id)
    assert len(rows) == 1, "the duplicate attempt must not write a second row"
    assert await _denied_count(db, object_id=obj_id, action=service.SIGNATURE_CREATE_SIMPLE) == 1


async def test_sign_and_sign_simple_share_the_one_per_purpose_guard(db):
    """The duplicate check is factored out and shared (`_refuse_duplicate_
    purpose`) — a valid SIMPLE signature must block a later ERI attempt on
    the same purpose exactly as it would block a second simple one:
    `uq_signatures_valid_purpose` does not care which kind got there first."""
    holder = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()

    await service.sign_simple(
        db, object_type="permit", object_id=obj_id, purpose=PURPOSE, document=DOC, user=holder
    )
    await db.commit()

    assert holder.pinfl is not None
    # A fresh certificate identity, never a fixed literal: `bind_certificate`
    # refuses a certificate already bound to an EARLIER run's user as
    # `certificate_owned_by_another_user` on this shared, persistent test DB
    # (lesson) — an unrelated failure that has nothing to do with the
    # duplicate-purpose guard this test actually exists to prove.
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"SER-{uuid.uuid4().hex[:12]}",
        issuer=f"ISS-{uuid.uuid4().hex[:8]}",
        pinfl=holder.pinfl,
    )
    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose=PURPOSE,
            document=DOC,
            pkcs7=pkcs7,
            user=holder,
        )
    assert exc.value.code == "ERR-SIGN-002"

    rows = await service.get_for_object(db, object_type="permit", object_id=obj_id)
    assert len(rows) == 1
    assert rows[0].kind == "simple"


async def test_reverify_of_a_simple_row_is_a_no_op(db):
    """Ruling #183: nothing cryptographic was ever checked, so this returns
    the SAME row unchanged — no new record, no certificate lookup (there is
    none to look up: `certificate_id` is NULL by construction)."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()
    original = await service.sign_simple(
        db, object_type="permit", object_id=obj_id, purpose=PURPOSE, document=DOC, user=user
    )
    await db.commit()

    result = await service.reverify(db, signature_id=original.id, user=user)
    assert result.id == original.id
    assert result.kind == "simple"
    assert result.verification_status == "valid"
    assert result.verification == original.verification

    rows = await service.get_for_object(db, object_type="permit", object_id=obj_id)
    assert len(rows) == 1, "reverify of a simple row must write no new evidence row"


async def test_list_signatures_page_kind_filter(db):
    """The repo-level filter `GET /signatures?kind=` rides on
    (`tests/modules/permits/test_simple_signature.py` drives the same thing
    through the real route)."""
    user = await make_user(db, role_code="applicant", pinfl=_pinfl())
    obj_id = uuid.uuid4()
    await service.sign_simple(
        db, object_type="permit", object_id=obj_id, purpose=PURPOSE, document=DOC, user=user
    )
    assert user.pinfl is not None
    pkcs7 = encode_mock_signature(
        document=DOC,
        serial=f"SER-{uuid.uuid4().hex[:12]}",
        issuer=f"ISS-{uuid.uuid4().hex[:8]}",
        pinfl=user.pinfl,
    )
    await service.sign(
        db,
        object_type="permit",
        object_id=obj_id,
        purpose="permit_head",
        document=DOC,
        pkcs7=pkcs7,
        user=user,
    )
    await db.commit()

    params = PageParams(page=1, page_size=50)
    simple_only, simple_total = await service.list_signatures_page(
        db, object_type="permit", object_id=obj_id, user=user, params=params, kind="simple"
    )
    assert simple_total == 1
    assert [row.kind for row in simple_only] == ["simple"]

    eri_only, eri_total = await service.list_signatures_page(
        db, object_type="permit", object_id=obj_id, user=user, params=params, kind="eri"
    )
    assert eri_total == 1
    assert [row.kind for row in eri_only] == ["eri"]

    both, both_total = await service.list_signatures_page(
        db, object_type="permit", object_id=obj_id, user=user, params=params
    )
    assert both_total == 2
    assert {row.kind for row in both} == {"eri", "simple"}


async def test_signature_model_check_constraint(db):
    """`kind`/`certificate_id`'s pair CHECK (migration 0052) is a DB-level
    invariant this module's own service always respects — proven here at the
    ORM boundary rather than trusted from the migration alone, mirroring how
    `tests/modules/signatures/test_models.py` proves the other constraints on
    this table."""
    bad = Signature(
        object_type="permit",
        object_id=uuid.uuid4(),
        purpose="permit_recipient",
        signer_user_id=None,
        certificate_id=None,
        doc_hash="deadbeef",
        signature_value="",
        signed_at=datetime.now(UTC),
        verification={},
        verification_status="valid",
        kind="eri",  # eri MUST carry a certificate_id — this row has none
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
