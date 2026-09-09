"""`GET /api/v1/dashboard/kpi` — real sources only (plan ruling g), zone
scoping, and the reversed-period guard (`ERR-VAL-001`, reused rather than a
module error code of its own)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from app.modules.dashboard.permissions import DASHBOARD_VIEW
from app.modules.gis.models import ContourVersion
from app.modules.payments.models import BUDGET_RECIPIENT_ID, Allocation, Invoice
from app.modules.permits.models import Permit
from tests.modules.gis.conftest import _client_for, make_contour, make_version, random_box_wkt
from tests.modules.gis.conftest import approval_doc as approval_doc  # noqa: F401
from tests.modules.gis.conftest import contours_layer as contours_layer  # noqa: F401
from tests.modules.inspections.conftest import application as application  # noqa: F401
from tests.modules.inspections.conftest import (
    default_checklist_id as default_checklist_id,  # noqa: F401,E501
)
from tests.modules.inspections.conftest import inspector as inspector  # noqa: F401
from tests.modules.inspections.conftest import inspector_client as inspector_client  # noqa: F401
from tests.modules.inspections.conftest import vt_01 as vt_01  # noqa: F401
from tests.modules.oversight.conftest import make_bare_application
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id  # noqa: F401
from tests.modules.permits.conftest import make_permit_on_contour

API = "/api/v1"

PERIOD_FROM = date(2027, 5, 1)
PERIOD_TO = date(2027, 9, 30)


async def _client_for_zoned(db, organization_id):
    async for client in _client_for(db, DASHBOARD_VIEW, organization_id=organization_id):
        yield client


async def _issue_within_period(db, *, contour, org, activity_type_id, status="active"):
    await make_version(db, contour.id, random_box_wkt())
    version_id = (
        await db.execute(select(ContourVersion.id).where(ContourVersion.contour_id == contour.id))
    ).scalar_one()
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version_id,
        org=org,
        activity_type_id=activity_type_id,
        status=status,
        period_from=PERIOD_FROM,
        period_to=PERIOD_TO,
    )
    row = await db.get(Permit, permit.id)
    row.issued_at = datetime(2027, 6, 1, tzinfo=UTC)
    await db.flush()
    return permit


async def test_kpi_counts_own_zone_and_has_no_omitted_tiles(
    db, leshoz, other_leshoz, contours_layer, grazing_activity_id
):
    """`inspections_count`/`violations_count` used to be the two names in
    `omitted` — stale the moment 4.1 `inspections` merged (seam audit,
    2026-09-06: `repo.inspections_kpi` gives both a real source). Every KPI
    tile now has one, so `omitted` is empty; `test_kpi_inspections_tile_is_
    zone_scoped` below proves the two new counts themselves, not just their
    absence from `omitted`."""
    contour = await make_contour(db, contours_layer, leshoz)
    await _issue_within_period(
        db, contour=contour, org=leshoz, activity_type_id=grazing_activity_id
    )

    other_contour = await make_contour(db, contours_layer, other_leshoz)
    await _issue_within_period(
        db, contour=other_contour, org=other_leshoz, activity_type_id=grazing_activity_id
    )

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["permits"]["issued_count"] == 1
        assert body["permits"]["active_count"] == 1
        assert body["omitted"] == []
        assert body["inspections"] == {"inspections_count": 0, "violations_count": 0}


async def test_kpi_inspections_tile_is_zone_scoped(
    db,
    leshoz,
    other_leshoz,
    application,
    inspector,
    inspector_client,
    default_checklist_id,
    vt_01,
):
    """Cross-module: a real field act signed as a violation
    (`inspections`, 4.1) must move `dashboard`'s new `inspections`/
    `violations_count` tiles (4.4) — and stay OUT of a different
    leshoz's own KPI, the same zone_filter every other tile here uses.
    Nothing before this seam audit ever ran these two modules together."""
    from app.modules.inspections import repo as inspections_repo
    from app.modules.inspections import service as inspections_service
    from app.modules.integrations.adapters.eimzo import encode_mock_signature

    created = await inspector_client.post(
        f"{API}/inspections/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": False},
            "result": "violation",
        },
    )
    assert created.status_code == 201, created.text
    act_id = created.json()["id"]
    act = await inspections_repo.get_act(db, uuid.UUID(act_id))
    assert act is not None

    pkcs7 = encode_mock_signature(
        document=inspections_service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(
        f"{API}/inspections/acts/{act_id}/sign",
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
    )
    assert signed.status_code == 200, signed.text
    assert await inspections_repo.case_for_act(db, act.id) is not None

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
        assert response.status_code == 200
        assert response.json()["inspections"] == {
            "inspections_count": 1,
            "violations_count": 1,
        }

    # The other leshoz's own dashboard must see neither — `zone_filter`,
    # not a filter this tile happens to skip.
    async for client in _client_for_zoned(db, other_leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
        assert response.status_code == 200
        assert response.json()["inspections"] == {
            "inspections_count": 0,
            "violations_count": 0,
        }


async def test_kpi_reversed_period_is_refused(db, leshoz):
    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": "2027-09-30", "period_to": "2027-05-01"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ERR-VAL-001"


async def test_kpi_applications_by_status(db, leshoz):
    app_row = await make_bare_application(db, org=leshoz)

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": "2027-01-01", "period_to": "2027-01-31"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["applications"]["by_status"].get(app_row.status, 0) >= 1


async def test_kpi_budget_share_reads_the_seeded_budget_recipient(db, leshoz):
    """Override 1 of stage 7.9 task 8 (decision #154): `budget_share_amount`
    used to sum `allocations.target == "budget"` — a value migration
    `0046` removed from `target_valid` entirely, so this KPI silently
    reported ZERO on every database migrated past that point, with no test
    ever failing (the exact defect this test closes). It now reads the
    seeded budget recipient's OWN share by `recipient_id ==
    BUDGET_RECIPIENT_ID`, never by a `target` string; `recipient_share_
    amount` stays a `target` read, since the leshoz's own remainder is
    unambiguous regardless of how many receivers are configured."""
    application = await make_bare_application(db, org=leshoz)
    invoice = Invoice(
        application_id=application.id,
        number=f"INV-KPI-{uuid.uuid4().hex[:8]}",
        amount=Decimal("600000.00"),
        status="paid",
        issued_at=datetime(2027, 6, 1, tzinfo=UTC),
        paid_at=datetime(2027, 6, 1, tzinfo=UTC),
    )
    db.add(invoice)
    await db.flush()
    db.add_all(
        [
            Allocation(
                invoice_id=invoice.id,
                entry_type="payment",
                target="receiver",
                recipient_id=BUDGET_RECIPIENT_ID,
                amount=Decimal("300000.00"),
            ),
            Allocation(
                invoice_id=invoice.id,
                entry_type="payment",
                target="recipient",
                amount=Decimal("300000.00"),
            ),
        ]
    )
    await db.flush()

    async for client in _client_for_zoned(db, leshoz.id):
        response = await client.get(
            f"{API}/dashboard/kpi",
            params={"period_from": PERIOD_FROM.isoformat(), "period_to": PERIOD_TO.isoformat()},
        )
        assert response.status_code == 200
        body = response.json()["payments"]
        assert body["invoiced_amount"] == "600000.00"
        assert body["paid_amount"] == "600000.00"
        assert body["budget_share_amount"] == "300000.00"
        assert body["recipient_share_amount"] == "300000.00"
