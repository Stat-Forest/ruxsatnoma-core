"""`GET /api/v1/oversight/*` — permission gate, zone scoping, and the
per-view audit trail С22 requires beyond this codebase's usual write-only
convention."""

from sqlalchemy import select

from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.oversight import service
from app.modules.oversight.permissions import OVERSIGHT_VIEW
from tests.modules.gis.conftest import _client_for
from tests.modules.oversight.conftest import make_bare_application

API = "/api/v1"


async def _client_for_zoned(db, organization_id, *, grant: bool = True):
    permissions = (OVERSIGHT_VIEW,) if grant else ()
    async for client in _client_for(db, *permissions, organization_id=organization_id):
        yield client


async def test_zone_scoped_client_sees_only_its_own_organization(db, leshoz, other_leshoz):
    mine = await make_bare_application(db, org=leshoz)
    theirs = await make_bare_application(db, org=other_leshoz)
    await audit.log(
        db,
        action="application.read",
        object_type="application",
        object_id=mine.id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": "RI-12"},
    )
    await audit.log(
        db,
        action="application.read",
        object_type="application",
        object_id=theirs.id,
        result="denied",
        basis="out_of_zone",
        extra={"risk_indicator": "RI-12"},
    )
    await service.harvest(db)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(mine.id)}
        )
        assert response.status_code == 200
        object_ids = {item["object_id"] for item in response.json()["items"]}
        assert str(mine.id) in object_ids

        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(theirs.id)}
        )
        assert response.json()["items"] == []


async def test_republic_wide_client_sees_both(db, leshoz, other_leshoz):
    mine = await make_bare_application(db, org=leshoz)
    theirs = await make_bare_application(db, org=other_leshoz)
    await audit.log(
        db,
        action="application.read",
        object_type="application",
        object_id=mine.id,
        extra={"risk_indicator": "RI-12"},
    )
    await audit.log(
        db,
        action="application.read",
        object_type="application",
        object_id=theirs.id,
        extra={"risk_indicator": "RI-12"},
    )
    await service.harvest(db)

    async for client in _client_for_zoned(db, None):
        for object_id in (mine.id, theirs.id):
            response = await client.get(
                f"{API}/oversight/risk-indicators", params={"object_id": str(object_id)}
            )
            object_ids = {item["object_id"] for item in response.json()["items"]}
            assert str(object_id) in object_ids


async def test_a_view_without_the_permission_is_refused(db):
    async for client in _client_for_zoned(db, None, grant=False):
        response = await client.get(f"{API}/oversight/risk-indicators")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ERR-ACL-001"


async def test_a_list_call_is_itself_audited(db, leshoz):
    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(f"{API}/oversight/risk-indicators")
        assert response.status_code == 200

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "oversight.view")))
        .scalars()
        .all()
    )
    assert len(rows) >= 1
