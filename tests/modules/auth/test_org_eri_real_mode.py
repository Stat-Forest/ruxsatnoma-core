"""Gap found in Task 4's own review, closed here: `_verify_org_challenge`
matched `identity.challenge` against an `otp_codes` row UNCONDITIONALLY.
In `real` mode e-imzo-server has already matched its own challenge, inside
`/backend/auth`, before ever answering `status: 1` -- and `identity.challenge`
is deliberately `""` in that mode (`RealEimzo.verify_signed_challenge`'s own
docstring, `login_via_eimzo`'s identical ruling R1 fix) -- so the
unconditional lookup always missed and refused every legal-entity
`org_eri` attach the instant `EIMZO_MODE=real` was set, forever.

Mirrors `test_eimzo_provider_outage.py`'s own approach: `_verify_org_challenge`
is exercised directly, with `get_eimzo_adapter` (and, here, `get_settings`)
monkeypatched in place -- the same shape `test_eimzo_login.py`'s own
`_StubAdapter`/`_OutageAdapter` use for `issue_eimzo_challenge`.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import DomainError
from app.modules.auth import service as auth_service
from app.modules.integrations.adapters.eimzo import EimzoError, EimzoIdentity

REAL_SETTINGS = Settings(
    eimzo_mode="real",
    eimzo_site_host="admin.ruxsatnoma-urmon.uz",
    _env_file=None,  # pyright: ignore[reportCallIssue]
)


class _StubOrgAdapter:
    """Stands in for `RealEimzo.verify_signed_challenge`: answers with an
    identity naming `tin`/`pinfl` and, per that method's own real-mode
    contract, `challenge=""` -- the response carries no challenge field to
    echo back at all. `ok=False` instead raises the same `ERR-AUTH-004` the
    real adapter raises for a non-1 status / undecodable envelope."""

    def __init__(self, *, tin: str, pinfl: str, ok: bool = True) -> None:
        self._tin = tin
        self._pinfl = pinfl
        self._ok = ok

    async def verify_signed_challenge(
        self, signed_challenge: str, ip: str | None = None
    ) -> EimzoIdentity:
        if not self._ok:
            raise EimzoError("ERR-AUTH-004")
        return EimzoIdentity(
            challenge="",
            pinfl=self._pinfl,
            full_name="DIRECTOR",
            tin=self._tin,
            legal_name="OOO ORG",
        )


@pytest.mark.asyncio
async def test_org_cert_attaches_in_real_mode_without_a_local_challenge_lookup(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `eimzo_challenge` row is ever created in this test -- if
    `_verify_org_challenge` still consulted `otp_codes` unconditionally, the
    lookup would miss and raise `ERR-ACL-001`/"challenge invalid" before ever
    reaching the tin/pinfl check below. Succeeding here proves the lookup was
    skipped, not merely that it happened to pass."""
    stir, pinfl = "123456789", "12345678901234"
    monkeypatch.setattr(auth_service, "get_settings", lambda: REAL_SETTINGS)
    monkeypatch.setattr(
        auth_service, "get_eimzo_adapter", lambda: _StubOrgAdapter(tin=stir, pinfl=pinfl)
    )

    identity = await auth_service._verify_org_challenge(
        db, signed_challenge="whatever-the-stub-ignores-it", stir=stir, signer_pinfl=pinfl, ip=None
    )

    assert identity.tin == stir
    assert identity.pinfl == pinfl


@pytest.mark.asyncio
async def test_a_genuinely_bad_signature_is_still_refused_in_real_mode(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix narrows WHICH lookup is skipped -- it must not also stop
    refusing an outright bad signature. Mirrors
    `test_eimzo_provider_outage.py::test_a_genuine_signature_refusal_still_becomes_bad_signature`,
    forced into `real` mode instead of relying on the default."""
    monkeypatch.setattr(auth_service, "get_settings", lambda: REAL_SETTINGS)
    monkeypatch.setattr(
        auth_service,
        "get_eimzo_adapter",
        lambda: _StubOrgAdapter(tin="123456789", pinfl="12345678901234", ok=False),
    )

    with pytest.raises(DomainError) as exc:
        await auth_service._verify_org_challenge(
            db,
            signed_challenge="broken",
            stir="123456789",
            signer_pinfl="12345678901234",
            ip=None,
        )

    assert exc.value.code == "ERR-ACL-001"
    assert exc.value.details is not None
    assert exc.value.details["reason"] == "bad signature"


class _RefusalWithReasonAdapter:
    """Stands in for a `RealEimzo` whose `_send` attached the provider's own
    `provider_status`/`reason` onto the `EimzoError` it raised — the
    ERR-INT-002 shape, distinct from `_StubOrgAdapter(ok=False)`'s
    ERR-AUTH-004 (a genuine bad-signature refusal, which has no provider
    status to carry)."""

    async def verify_signed_challenge(
        self, signed_challenge: str, ip: str | None = None
    ) -> EimzoIdentity:
        raise EimzoError("ERR-INT-002", provider_status=-11, reason="certificate_invalid")


@pytest.mark.asyncio
async def test_an_integration_error_carries_the_providers_reason(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Minor 9 (final review): the `exc.err_code != "ERR-AUTH-004"` branch
    used to `raise err(exc.err_code)` with no `details`, discarding a
    genuine `provider_status`/`.reason` an ERR-INT-002 carries."""
    monkeypatch.setattr(auth_service, "get_settings", lambda: REAL_SETTINGS)
    monkeypatch.setattr(auth_service, "get_eimzo_adapter", lambda: _RefusalWithReasonAdapter())

    with pytest.raises(DomainError) as exc:
        await auth_service._verify_org_challenge(
            db,
            signed_challenge="whatever",
            stir="123456789",
            signer_pinfl="12345678901234",
            ip=None,
        )

    assert exc.value.code == "ERR-INT-002"
    assert exc.value.details == {"provider_status": -11, "reason": "certificate_invalid"}
