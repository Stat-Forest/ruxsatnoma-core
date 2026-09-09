"""Fix round 1, finding 1: a provider transport failure or non-200
(`EimzoError`, `ERR-INT-001`/`ERR-INT-002`) reaching `sign()` or
`register_certificate()` must surface as the mapped integration error --
never left to escape uncaught into `app/main.py`'s catch-all, which would
answer a bare 500 `ERR-SYS-001` and tell a citizen the whole system is
broken rather than that E-IMZO is unreachable. It must also never be
recorded as a verdict about the signature itself: nothing was ever
evaluated, so no `signatures` row and (fix round 1's own decision, see the
docstrings on `sign()`/`register_certificate()`) no `audit_log` entry
either -- mirroring `auth.service.login_via_eimzo`'s identical handling of
the same exception one call site up.
"""

import secrets
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.integrations.adapters.eimzo import EimzoError, EimzoVerification
from app.modules.signatures import service
from app.modules.signatures.models import Certificate, Signature
from tests.modules.auth.test_sessions import make_user

DOC = b"the-permit-bytes"


def _pinfl() -> str:
    return f"{secrets.randbelow(10**14):014d}"


class _OutageAdapter:
    """Stands in for `get_eimzo_adapter()`: every call to the provider fails
    the transport/non-200 way `RealEimzo._send` does -- never a verdict, a
    genuine exception."""

    async def verify_detached(
        self, *, document: bytes, pkcs7: str, ip: str | None = None
    ) -> EimzoVerification:
        raise EimzoError("ERR-INT-001")

    async def verify_attached(self, pkcs7: str, ip: str | None = None) -> EimzoVerification:
        raise EimzoError("ERR-INT-001")


@pytest.fixture
async def a_user(db: AsyncSession) -> User:
    return await make_user(db, pinfl=_pinfl())


@pytest.mark.asyncio
async def test_sign_surfaces_a_provider_outage_as_the_integration_error(
    db: AsyncSession, a_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _OutageAdapter())
    obj_id = uuid.uuid4()

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=obj_id,
            purpose="permit_head",
            document=DOC,
            pkcs7="unused-the-fake-adapter-never-decodes-it",
            user=a_user,
        )

    # Mapped to its own registered code/status (503), never left to escape
    # as an unhandled exception app/main.py's catch-all would turn into a
    # bare 500 ERR-SYS-001.
    assert exc.value.code == "ERR-INT-001"
    assert exc.value.http_status == 503

    # Not a verdict about THIS signature: no `signatures` row at all (valid
    # or invalid) for this object -- the provider never told us anything
    # about the envelope.
    signature_count = (
        await db.execute(
            select(func.count()).select_from(Signature).where(Signature.object_id == obj_id)
        )
    ).scalar_one()
    assert signature_count == 0

    # And, the fix round's own decision: no audit entry either -- an outage
    # says nothing about the signer, so it earns no evidence row (contrast
    # with every OTHER refusal in `sign()`, which always audits before
    # raising).
    audit_count = (
        await db.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.object_id == obj_id)
        )
    ).scalar_one()
    assert audit_count == 0


@pytest.mark.asyncio
async def test_register_certificate_surfaces_a_provider_outage_as_the_integration_error(
    db: AsyncSession, a_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _OutageAdapter())

    with pytest.raises(DomainError) as exc:
        await service.register_certificate(
            db, pkcs7="unused-the-fake-adapter-never-decodes-it", user=a_user
        )

    assert exc.value.code == "ERR-INT-001"
    assert exc.value.http_status == 503

    # No `certificates` row bound to this user -- an outage proves nothing
    # about the presentation.
    cert_count = (
        await db.execute(
            select(func.count()).select_from(Certificate).where(Certificate.user_id == a_user.id)
        )
    ).scalar_one()
    assert cert_count == 0

    audit_count = (
        await db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == service.CERTIFICATE_BIND, AuditLog.user_id == a_user.id)
        )
    ).scalar_one()
    assert audit_count == 0


class _RefusalWithReasonAdapter:
    """Stands in for a `RealEimzo` whose `_send` attached the provider's own
    `provider_status`/`reason` onto the `EimzoError` it raised (`_send`'s own
    non-200 handling does exactly this) -- unlike `_OutageAdapter` above,
    which stands in for the transport-failure shape that carries neither."""

    async def verify_detached(
        self, *, document: bytes, pkcs7: str, ip: str | None = None
    ) -> EimzoVerification:
        raise EimzoError("ERR-INT-002", provider_status=-11, reason="certificate_invalid")


@pytest.mark.asyncio
async def test_sign_carries_the_providers_reason_onto_the_raised_error(
    db: AsyncSession, a_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor 9 (final review): `sign()` used to `raise err(exc.err_code)`
    with no `details`, discarding `EimzoError.provider_status`/`.reason` --
    the exact payload `integrations.service.eimzo_error_details` already
    builds for the timestamp route. A 502 that could have said "certificate
    invalid" told the citizen nothing at all."""
    monkeypatch.setattr(service, "get_eimzo_adapter", lambda: _RefusalWithReasonAdapter())

    with pytest.raises(DomainError) as exc:
        await service.sign(
            db,
            object_type="permit",
            object_id=uuid.uuid4(),
            purpose="permit_head",
            document=DOC,
            pkcs7="unused-the-fake-adapter-never-decodes-it",
            user=a_user,
        )

    assert exc.value.code == "ERR-INT-002"
    assert exc.value.details == {"provider_status": -11, "reason": "certificate_invalid"}
