"""`GET /search`. The zone-scoping tests here are THE test this whole track
must not fail (track brief: "every list `search` touches must keep
territorial scoping" — an actor scoped to one organization must not find
another's rows through search)."""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import uuid7
from app.modules.admin.models import Organization
from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.gis.models import GisLayer
from app.modules.search.permissions import SEARCH_USE
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
from tests.modules.permits.conftest import unique_pinfl
from tests.modules.search.conftest import (
    _client_for,
    _client_with_role,
    make_application,
    make_permit_on_contour,
)


async def test_search_requires_the_permission(db: AsyncSession, leshoz: Organization):
    async for client in _client_with_role(db, "inspector"):  # a role search.use is NOT granted to
        resp = await client.get("/api/v1/search", params={"kind": "applications"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_zone_scoped_actor_does_not_see_another_orgs_application(
    db: AsyncSession, leshoz: Organization, other_leshoz: Organization
):
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Иванов Пётр")
    other = await make_application(
        db, org=other_leshoz, status="CLOSED", applicant_name="Петров Иван"
    )
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get("/api/v1/search", params={"kind": "applications", "page_size": 100})
        assert resp.status_code == 200
        body = resp.json()
        ids = {row["id"] for row in body["items"]}
        assert str(other.id) not in ids, "search leaked another organization's application"


async def test_republic_wide_actor_sees_both_organizations(
    db: AsyncSession, leshoz: Organization, other_leshoz: Organization
):
    a = await make_application(db, org=leshoz, status="CLOSED", applicant_name="Алиев Вали")
    b = await make_application(db, org=other_leshoz, status="CLOSED", applicant_name="Валиев Али")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE):  # no organization_id -> republic-wide
        resp = await client.get("/api/v1/search", params={"kind": "applications", "page_size": 100})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert {str(a.id), str(b.id)} <= ids


async def test_zone_scoped_actor_finds_own_unassigned_application_via_its_contour(
    db: AsyncSession,
    leshoz: Organization,
    other_leshoz: Organization,
    contours_layer: GisLayer,
    grazing_activity_id: uuid.UUID,
    approval_doc,
):
    """Seam audit, 2026-09-06: `Application.assigned_org_id` is null for every
    DRAFT and stays null through SUBMITTED (`applications.service`'s own
    documented reasoning) — a SUBMITTED, not-yet-assigned application still
    belongs to its CONTOUR's organization, and `dashboard`/`oversight`/
    `applications` itself all count it there. `search` used to scope on
    `assigned_org_id` alone and could never find it, so a leshoz's own staff
    could not find their OWN unassigned applications through search."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    submitter = await make_user(db)
    applicant = Applicant(kind="individual", pinfl=unique_pinfl(), name="Юсупов Акмал")
    db.add(applicant)
    await db.flush()
    application = Application(
        id=uuid7(),
        number=f"APP-{uuid.uuid4().hex[:8]}",
        applicant_id=applicant.id,
        submitted_by_user_id=submitter.id,
        on_behalf="self",
        activity_type_id=grazing_activity_id,
        contour_id=contour.id,
        status="SUBMITTED",
        channel="portal",
        assigned_org_id=None,
        period_from=date(2027, 5, 1),
        period_to=date(2027, 9, 30),
    )
    db.add(application)
    await db.flush()
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get("/api/v1/search", params={"kind": "applications", "page_size": 100})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert str(application.id) in ids, (
            "search hid a leshoz's own unassigned application, resolvable only "
            "through its contour's owner"
        )

    # A DIFFERENT leshoz's own search must still exclude it — the fix scopes,
    # it does not stop scoping.
    async for client in _client_for(db, SEARCH_USE, organization_id=other_leshoz.id):
        resp = await client.get("/api/v1/search", params={"kind": "applications", "page_size": 100})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert str(application.id) not in ids


async def test_text_search_finds_by_applicant_name(db: AsyncSession, leshoz: Organization):
    match = await make_application(
        db, org=leshoz, status="CLOSED", applicant_name="Шарипова Гулнора"
    )
    other = await make_application(db, org=leshoz, status="CLOSED", applicant_name="Каримов Botir")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get("/api/v1/search", params={"kind": "applications", "q": "Гулнора"})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert str(match.id) in ids
        assert str(other.id) not in ids


async def test_status_filter_narrows_results(db: AsyncSession, leshoz: Organization):
    closed = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    cancelled = await make_application(db, org=leshoz, status="CANCELLED", applicant_name="B B")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get(
            "/api/v1/search", params={"kind": "applications", "status": "CLOSED", "page_size": 100}
        )
        ids = {row["id"] for row in resp.json()["items"]}
        assert str(closed.id) in ids
        assert str(cancelled.id) not in ids


async def test_zone_scoped_actor_does_not_see_another_orgs_permit(
    db: AsyncSession,
    leshoz: Organization,
    other_leshoz: Organization,
    contours_layer,
    grazing_activity_id: uuid.UUID,
    approval_doc,
):
    contour_a = await make_contour(db, contours_layer, leshoz)
    version_a = await make_version(
        db, contour_a.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    contour_b = await make_contour(db, contours_layer, other_leshoz)
    version_b = await make_version(
        db, contour_b.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )

    mine = await make_permit_on_contour(
        db,
        contour=contour_a,
        version_id=version_a.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="revoked",
    )
    theirs = await make_permit_on_contour(
        db,
        contour=contour_b,
        version_id=version_b.id,
        org=other_leshoz,
        activity_type_id=grazing_activity_id,
        status="revoked",
    )
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get("/api/v1/search", params={"kind": "permits", "page_size": 100})
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()["items"]}
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids


# --- a permit is searched by the number a person is actually shown ------------
#
# Stage 7.3 finding F21: `search_permits` built its display number as
# `"<series>-<number>"`, so a permit whose document, whose card and whose public
# check page all read `А № 000003` was found by typing `А-3` and by nothing
# else. A prosecutor or an inspector types what is on the paper.


async def test_a_permit_is_found_by_the_number_printed_on_it(
    db: AsyncSession,
    leshoz: Organization,
    contours_layer,
    grazing_activity_id: uuid.UUID,
    approval_doc,
):
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
    )
    await db.commit()

    printed = f"{permit.series} № {permit.number:06d}"
    padded = f"{permit.number:06d}"

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        for query in (printed, padded, str(permit.number)):
            resp = await client.get(
                "/api/v1/search", params={"kind": "permits", "q": query, "page_size": 100}
            )
            assert resp.status_code == 200, resp.text
            ids = {row["id"] for row in resp.json()["items"]}
            assert str(permit.id) in ids, query


async def test_the_search_result_shows_the_printed_number_not_an_internal_form(
    db: AsyncSession,
    leshoz: Organization,
    contours_layer,
    grazing_activity_id: uuid.UUID,
    approval_doc,
):
    """`permits.service._permit_number` is what the document and every
    notification print; a result list spelling the same identifier a second way
    is how a person decides they found a different permit."""
    contour = await make_contour(db, contours_layer, leshoz)
    version = await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    permit = await make_permit_on_contour(
        db,
        contour=contour,
        version_id=version.id,
        org=leshoz,
        activity_type_id=grazing_activity_id,
        status="active",
    )
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.get(
            "/api/v1/search",
            params={"kind": "permits", "q": f"{permit.number:06d}", "page_size": 100},
        )
        row = next(r for r in resp.json()["items"] if r["id"] == str(permit.id))
        assert row["number"] == f"{permit.series} № {permit.number:06d}"
