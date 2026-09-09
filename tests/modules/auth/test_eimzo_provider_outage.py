"""Fix round 1, finding 3: `_verify_org_challenge` used to map EVERY
`EimzoError` to `ERR-ACL-001` ("bad signature"). Now that the adapter can
raise `ERR-INT-001`/`ERR-INT-002` for a provider outage, collapsing it into
the same code told a citizen their organisation certificate was invalid when
the real story was that E-IMZO could not be reached. An integration error
must keep its own code; only a genuine signature refusal (the unchanged
login contract's `ERR-AUTH-004`) becomes `ERR-ACL-001`.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.modules.auth import service as auth_service
from app.modules.integrations.adapters.eimzo import EimzoError, EimzoIdentity


class _OutageAdapter:
    async def verify_signed_challenge(
        self, signed_challenge: str, ip: str | None = None
    ) -> EimzoIdentity:
        raise EimzoError("ERR-INT-001")


class _RefusingAdapter:
    async def verify_signed_challenge(
        self, signed_challenge: str, ip: str | None = None
    ) -> EimzoIdentity:
        raise EimzoError("ERR-AUTH-004")


@pytest.mark.asyncio
async def test_a_provider_outage_keeps_its_own_code_instead_of_becoming_bad_signature(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_service, "get_eimzo_adapter", lambda: _OutageAdapter())

    with pytest.raises(DomainError) as exc:
        await auth_service._verify_org_challenge(
            db,
            signed_challenge="x",
            stir="123456789",
            signer_pinfl="12345678901234",
            ip=None,
        )

    assert exc.value.code == "ERR-INT-001"
    assert exc.value.http_status == 503


@pytest.mark.asyncio
async def test_a_genuine_signature_refusal_still_becomes_bad_signature(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the fix above: the unchanged login contract's
    `ERR-AUTH-004` (a non-1 status / undecodable envelope) must still map to
    `ERR-ACL-001` -- the fix narrows WHICH codes get relabelled, it must not
    stop relabelling this one."""
    monkeypatch.setattr(auth_service, "get_eimzo_adapter", lambda: _RefusingAdapter())

    with pytest.raises(DomainError) as exc:
        await auth_service._verify_org_challenge(
            db,
            signed_challenge="x",
            stir="123456789",
            signer_pinfl="12345678901234",
            ip=None,
        )

    assert exc.value.code == "ERR-ACL-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "bad signature"
