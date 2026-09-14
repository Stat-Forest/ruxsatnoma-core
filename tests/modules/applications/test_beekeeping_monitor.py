"""`GET /applications/beekeeping` (ruling #217): the Beekeeping Union's
registrar monitors every application claiming `beekeeping_union_member`,
country-wide, and sees nothing else. The claim is stamped on a real
committed application in-process (the same shape
`test_benefit_verification.py`'s last test uses) and read back through the
real route, so the gate, the classifier join and the narrow shape are all
exercised on the wire."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.applications import service
from app.modules.beekeepers.permissions import BEEKEEPERS_MANAGE
from tests.conftest import make_client
from tests.modules.applications.test_benefit_verification import _benefit_category_item_id
from tests.modules.beekeepers.test_router import auth_client, registrar_client

API = "/api/v1"


async def _stamp_claim(db: AsyncSession, application_id: str, code: str, certificate_no: str):
    application = await service.get(db, uuid.UUID(application_id))
    assert application is not None
    application.benefit_category_item_id = await _benefit_category_item_id(db, code)
    application.benefit_certificate_no = certificate_no
    application.benefit_verification_status = "pending"
    await db.commit()


async def test_requires_the_register_permission(db, submitted_application: str) -> None:
    _, token, csrf = await registrar_client(db)  # no grants
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/applications/beekeeping")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_lists_only_the_beekeeping_claims_in_the_narrow_shape(
    db, submitted_application: str
) -> None:
    certificate_no = f"BEE-{uuid.uuid4().hex[:10]}"
    await _stamp_claim(db, submitted_application, "beekeeping_union_member", certificate_no)
    _, token, csrf = await registrar_client(db, BEEKEEPERS_MANAGE)

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/applications/beekeeping", params={"page_size": 100})
        r_filtered = await client.get(
            f"{API}/applications/beekeeping", params={"status": "REJECTED", "page_size": 100}
        )
        # The same application re-stamped with a recreation claim: a
        # benefit, but not the Union's — it must be absent, not merely marked.
        await _stamp_claim(db, submitted_application, "preschool_children", "CERT-0009")
        r_other = await client.get(f"{API}/applications/beekeeping", params={"page_size": 100})
    assert r.status_code == 200, r.text
    by_id = {item["id"]: item for item in r.json()["items"]}
    assert submitted_application in by_id
    assert r_other.status_code == 200
    assert submitted_application not in {i["id"] for i in r_other.json()["items"]}
    row = by_id[submitted_application]
    assert row["benefit_certificate_no"] == certificate_no
    assert row["benefit_verification_status"] == "pending"
    assert row["status"] == "SUBMITTED"
    assert row["applicant_name"]
    # The registrar reads the claim and its fate, never the card: no
    # applicant id, no contour, no calculation.
    assert set(row) == {
        "id",
        "number",
        "status",
        "applicant_name",
        "organization_name",
        "benefit_certificate_no",
        "benefit_verification_status",
        "period_from",
        "period_to",
        "submitted_at",
        "decided_at",
    }
    assert r_filtered.status_code == 200
    assert submitted_application not in {i["id"] for i in r_filtered.json()["items"]}
