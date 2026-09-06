"""`GET /search`. The zone-scoping tests here are THE test this whole track
must not fail (track brief: "every list `search` touches must keep
territorial scoping" — an actor scoped to one organization must not find
another's rows through search)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.search.permissions import SEARCH_USE
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt
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
