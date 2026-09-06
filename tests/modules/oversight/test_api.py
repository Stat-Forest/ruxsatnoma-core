"""`GET /api/v1/oversight/*` — permission gate, zone scoping, and the
per-view audit trail С22 requires beyond this codebase's usual write-only
convention."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.modules.audit import service as audit
from app.modules.audit.models import AuditLog
from app.modules.inspections.models import InspectionAct
from app.modules.oversight import service
from app.modules.oversight.permissions import OVERSIGHT_VIEW
from app.modules.reports.models import Report, ReportForm
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import _client_for
from tests.modules.inspections.conftest import (
    default_checklist_id as default_checklist_id,  # noqa: F401,E501
)
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


async def test_zone_scoped_client_sees_ri05_on_an_inspection_act(
    db, leshoz, other_leshoz, default_checklist_id
):
    """Seam audit, 2026-09-06: `object_type="inspection_act"` used to have no
    entry in `_resolved_organization_id()`'s `case()` — `inspections` is a
    level-5 sibling built in the same parallel wave as this module, and
    `inspection_acts.organization_id` is a direct column, not a "no natural
    owner" object type. RI-05 (a signature attempt on a revoked/expired
    certificate) is raised under whichever `object_type` the SIGNED document
    carries — an inspector's own field act is one of them."""
    inspector = await make_user(db, role_code="inspector", organization_id=leshoz.id)
    act = InspectionAct(
        organization_id=leshoz.id,
        inspector_id=inspector.id,
        occurred_at=datetime(2027, 6, 1, tzinfo=UTC),
        checklist_id=default_checklist_id,
        status="signed",
    )
    db.add(act)
    await db.flush()
    await audit.log(
        db,
        action="signature.create",
        object_type="inspection_act",
        object_id=act.id,
        result="denied",
        basis="certificate_revoked",
        extra={"risk_indicator": "RI-05", "reason": "certificate_revoked"},
    )
    await service.harvest(db)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(act.id)}
        )
        assert response.status_code == 200
        object_ids = {item["object_id"] for item in response.json()["items"]}
        assert str(act.id) in object_ids, (
            "a leshoz-scoped viewer must see RI-05 on its OWN inspector's act"
        )

    async for client in _client_for_zoned(db, other_leshoz.id):
        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(act.id)}
        )
        assert response.json()["items"] == []


async def test_zone_scoped_client_sees_ri05_on_a_report(db, leshoz, other_leshoz):
    """Same finding as the inspection-act test above, for `object_type=
    "report"` (`reports.organization_id` is likewise a direct column)."""
    submitter = await make_user(db, organization_id=leshoz.id)
    form = ReportForm(
        code=f"TEST-{uuid.uuid4().hex[:8]}",
        version=1,
        name={"uz_cyrl": "Тест", "ru": "Тест"},
        period_type="quarter",
        columns=[],
        created_by=submitter.id,
    )
    db.add(form)
    await db.flush()
    report = Report(
        form_id=form.id,
        organization_id=leshoz.id,
        period_start=date(2027, 1, 1),
        period_end=date(2027, 1, 31),
        created_by=submitter.id,
    )
    db.add(report)
    await db.flush()
    await audit.log(
        db,
        action="signature.create",
        object_type="report",
        object_id=report.id,
        result="denied",
        basis="certificate_expired",
        extra={"risk_indicator": "RI-05", "reason": "certificate_expired"},
    )
    await service.harvest(db)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(report.id)}
        )
        assert response.status_code == 200
        object_ids = {item["object_id"] for item in response.json()["items"]}
        assert str(report.id) in object_ids

    async for client in _client_for_zoned(db, other_leshoz.id):
        response = await client.get(
            f"{API}/oversight/risk-indicators", params={"object_id": str(report.id)}
        )
        assert response.json()["items"] == []


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
