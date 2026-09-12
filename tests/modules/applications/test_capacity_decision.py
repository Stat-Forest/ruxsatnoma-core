"""Ruling #176's DECISION-time refusal point (stage 9, T6): approving an
application re-runs the capacity/exclusivity check and refuses BEFORE an
invoice is raised, so a second applicant for an already-taken contour x
activity slot never pays for it.

Apiary, not grazing: `norms.checks._norm_check` is grazing-only (a norm-less
grazing draft fails `norm_available` before ever reaching the limit check at
all), so the EXCLUSIVE case — no norm, no capacity, ANY overlapping ACTIVE
permit blocks — is reachable here with no norm fixture at all. The pre-existing
occupant is built directly through the ORM as a `Permit` row, the shape
`tests/modules/permits/conftest.py::make_permit_on_contour` uses, since driving
a real issuance here would need this whole module's own paid/signed chain for
a fact only the PRE-CONDITION cares about.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.gis.models import Contour, ContourVersion
from app.modules.payments import service as payments_service
from app.modules.permits import repo as permits_repo
from app.modules.permits.models import Permit
from tests.modules.applications.test_decision import _decide
from tests.modules.applications.test_submit import _submit
from tests.modules.auth.test_sessions import make_user

API = "/api/v1"


def _unique_pinfl() -> str:
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def apiary_activity_id(db: AsyncSession) -> uuid.UUID:
    from sqlalchemy import text

    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'apiary'"))
    return rows.scalar_one()


async def _apiary_permit(
    db: AsyncSession,
    *,
    contour: Contour,
    org,
    activity_type_id: uuid.UUID,
    period_from: str,
    period_to: str,
    status: str = "active",
) -> Permit:
    """An unrelated applicant's permit, already occupying the contour ×
    activity slot — built directly, the same reasoning `make_permit_on_contour`
    documents (nothing in 3.11a writes `active` outside four real signatures,
    and this fixture exists to test the READ side of that fact, not to redo
    the signing flow). `status="pending_signatures"` is the state `issue()`
    writes and the permit may sit in indefinitely (ruling #99); its
    application is then still `PAID`, since only the last signature moves it
    to `PERMIT_ISSUED`."""
    user = await make_user(db, role_code="applicant", pinfl=_unique_pinfl())
    applicant = Applicant(
        kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id
    )
    db.add(applicant)
    await db.flush()

    version_id = await db.scalar(
        select(ContourVersion.id).where(ContourVersion.contour_id == contour.id)
    )

    application = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=user.id,
        on_behalf="self",
        activity_type_id=activity_type_id,
        contour_id=contour.id,
        contour_version_id=version_id,
        requested_area_ha=Decimal("2.0000"),
        period_from=date.fromisoformat(period_from),
        period_to=date.fromisoformat(period_to),
        status="PERMIT_ISSUED" if status == "active" else "PAID",
        channel="portal",
        assigned_org_id=org.id,
    )
    db.add(application)
    await db.flush()

    series = get_settings().permit_series
    number = await permits_repo.next_number(db, series)
    assert number is not None
    import secrets

    permit = Permit(
        series=series,
        number=number,
        application_id=application.id,
        applicant_id=applicant.id,
        activity_type_id=activity_type_id,
        organization_id=org.id,
        contour_id=contour.id,
        contour_version_id=version_id,
        area_ha=Decimal("2.0000"),
        period_from=date.fromisoformat(period_from),
        period_to=date.fromisoformat(period_to),
        amount=Decimal("100000.00"),
        sb_load=None,
        quantity=Decimal("3.0000"),
        status=status,
        qr_token=secrets.token_urlsafe(32),
        snapshot={"holder_name": "Existing Holder"},
    )
    db.add(permit)
    await db.flush()
    return permit


async def test_a_second_applicant_is_refused_at_approval_before_any_invoice(
    db: AsyncSession,
    other_applicant_client,
    hodim_client,
    executor_head_client,
    published_contour: Contour,
    leshoz,
    apiary_activity_id: uuid.UUID,
) -> None:
    """Ruling #176's own worked case: money must never be collected from a
    second applicant whose slot is already gone. `POST /approve` answers
    422 `ERR-NORM-002`, the application stays exactly where it was, and no
    invoice was ever raised for it.

    **The occupant appears AFTER this application was filed**, and that is the
    whole point of a second refusal point. A contour already taken when the
    applicant files is refused at SUBMISSION — the same check, blocking there
    too — so a test that occupies the contour first never reaches the decision
    at all; it was written that way and proved only the earlier gate. The case
    approval exists for is the one where the slot is lost in between: filed
    against a free contour, occupied while it sat in review.
    """
    filing = {
        "on_behalf": "self",
        "contour_id": str(published_contour.id),
        "activity_type_id": str(apiary_activity_id),
        "period_from": "2027-06-01",
        "period_to": "2027-07-31",
        "quantity": "5",
    }
    submitted = await _submit(other_applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    app_id = submitted.json()["id"]

    started = await hodim_client.post(f"{API}/applications/{app_id}/start-review")
    assert started.status_code == 200, started.text

    # The slot is lost WHILE the application sits in review — somebody else's
    # permit becomes active over the same contour, activity and period.
    occupant = await _apiary_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=apiary_activity_id,
        period_from="2027-05-01",
        period_to="2027-09-30",
    )
    await db.commit()

    result = await _decide(executor_head_client, app_id, "approve")
    assert result.status_code == 422, result.text
    body = result.json()["error"]
    assert body["code"] == "ERR-NORM-002"
    assert body["details"]["reason"] == "exclusive_occupied"
    assert body["details"]["occupied_until"] == "2027-09-30"

    # Still IN_REVIEW — no status change, and no invoice was raised for it.
    current = await other_applicant_client.get(f"{API}/applications/{app_id}")
    assert current.json()["status"] == "IN_REVIEW"
    assert await payments_service.invoice_for_application(db, uuid.UUID(app_id)) is None

    # The pre-existing occupant is untouched, for good measure.
    await db.refresh(occupant)
    assert occupant.status == "active"


async def test_a_second_applicant_is_refused_at_approval_while_the_first_permit_awaits_signatures(
    db: AsyncSession,
    other_applicant_client,
    hodim_client,
    executor_head_client,
    published_contour: Contour,
    leshoz,
    apiary_activity_id: uuid.UUID,
) -> None:
    """The gap between issuance and activation. The first applicant paid and
    `issue()` wrote their permit in `pending_signatures`, where it may wait
    for weeks (ruling #99). Issuance's own gate already counts that row
    (`OCCUPYING_STATUSES`); if the DECISION gate counted `active`
    only, this second applicant would be approved, invoiced and PAID, and
    refused only at issuance — exactly the manual refund ruling #176 exists
    to prevent. Same refusal, same reason, one status earlier."""
    filing = {
        "on_behalf": "self",
        "contour_id": str(published_contour.id),
        "activity_type_id": str(apiary_activity_id),
        "period_from": "2027-06-01",
        "period_to": "2027-07-31",
        "quantity": "5",
    }
    submitted = await _submit(other_applicant_client, filing)
    assert submitted.status_code == 201, submitted.text
    app_id = submitted.json()["id"]

    started = await hodim_client.post(f"{API}/applications/{app_id}/start-review")
    assert started.status_code == 200, started.text

    await _apiary_permit(
        db,
        contour=published_contour,
        org=leshoz,
        activity_type_id=apiary_activity_id,
        period_from="2027-05-01",
        period_to="2027-09-30",
        status="pending_signatures",
    )
    await db.commit()

    result = await _decide(executor_head_client, app_id, "approve")
    assert result.status_code == 422, result.text
    body = result.json()["error"]
    assert body["code"] == "ERR-NORM-002"
    assert body["details"]["reason"] == "exclusive_occupied"
    assert body["details"]["occupied_until"] == "2027-09-30"

    current = await other_applicant_client.get(f"{API}/applications/{app_id}")
    assert current.json()["status"] == "IN_REVIEW"
    assert await payments_service.invoice_for_application(db, uuid.UUID(app_id)) is None
