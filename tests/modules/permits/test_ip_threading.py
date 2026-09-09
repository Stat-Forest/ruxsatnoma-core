"""Fix round 1, finding 2: nothing pinned the `ip` threading from a router
down through `signatures.service.sign()` into the E-IMZO adapter -- dropping
`ip=...` from a router call is a silent regression no test caught, and
`X-Real-IP` is what the provider records in its own legally-significant
audit trail. `signatures.service.sign()` has eight such call sites
(task-3-report.md's own table); the permit-signing route is the most
consequential, so it is the one end-to-end proof this fix round adds.

This test builds its own signed-in client rather than reusing the module's
`head_client` fixture: proving the ACTUAL request address survives to the
adapter needs a DISTINGUISHABLE `X-Real-IP`, and every other client fixture
in this package is built through `make_client`'s own ('127.0.0.1', 123)
default -- indistinguishable from a hardcoded fallback.
"""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.integrations.adapters.eimzo import EimzoVerification, encode_mock_signature
from app.modules.permits.models import Permit
from app.modules.signatures import service as signatures_service
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.gis.conftest import _commit_pending_before_requests
from tests.modules.permits.conftest import unique_pinfl

SIGNER_IP = "203.0.113.42"


@pytest.mark.asyncio
async def test_permit_signature_route_threads_the_real_client_address_to_the_adapter(
    db: AsyncSession,
    leshoz: Organization,
    issued_permit: Permit,
    permit_pdf: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingAdapter:
        revocation_checkable = True

        async def verify_detached(
            self, *, document: bytes, pkcs7: str, ip: str | None
        ) -> EimzoVerification:
            captured["ip"] = ip
            # A plain refusal, never an `EimzoError` -- an ERR-SIGN-001
            # response is what proves the request reached `sign()` and the
            # adapter, not merely the router.
            return EimzoVerification(
                status_code=-10,
                subject_certificate=None,
                signed_at=None,
                timestamp_token=None,
                raw={},
            )

    monkeypatch.setattr(signatures_service, "get_eimzo_adapter", lambda: _CapturingAdapter())

    user = await make_user(
        db, role_code="executor_head", organization_id=leshoz.id, pinfl=unique_pinfl()
    )
    _, token, csrf = await make_session(db, user)
    await db.commit()
    assert user.pinfl is not None

    async with make_client(
        create_app(), lifespan=True, client_address=(SIGNER_IP, 51234)
    ) as client:
        auth_client(client, token, csrf)
        _commit_pending_before_requests(client, db)
        response = await client.post(
            f"/api/v1/permits/{issued_permit.id}/signatures",
            json={
                "purpose": "permit_head",
                "pkcs7": encode_mock_signature(
                    document=permit_pdf,
                    serial=f"SER-{user.pinfl}",
                    issuer="ISS-1",
                    pinfl=user.pinfl,
                ),
            },
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ERR-SIGN-001"
    # The whole point: the adapter received the REQUEST's own client
    # address, not `None` and not the default ('127.0.0.1') a dropped
    # `ip=...` router argument would leave behind.
    assert captured["ip"] == SIGNER_IP
