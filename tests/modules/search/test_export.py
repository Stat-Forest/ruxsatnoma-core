"""С22: `POST /search/exports` (decision #98, ruling #20). Four things this
track's brief names as the minimum: the row cap is enforced and configurable,
the watermark carries the operator's name and the date, one audit row is
written per export, and — the test this whole feature exists to pass — a
zone-scoped operator's export contains only their own zone's rows."""

import io
import uuid

from pypdf import PdfReader
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
from app.core.models import SystemSetting
from app.core.time import business_today
from app.modules.admin.models import Organization
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.search.permissions import SEARCH_USE
from tests.modules.search.conftest import _client_for, _client_with_role, make_application

API = "/api/v1"


async def test_export_requires_the_permission(db: AsyncSession, leshoz: Organization):
    async for client in _client_with_role(db, "inspector"):  # a role search.use is NOT granted to
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "xlsx"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_the_row_cap_is_enforced_and_configurable(db: AsyncSession, leshoz: Organization):
    for i in range(3):
        await make_application(db, org=leshoz, status="CLOSED", applicant_name=f"Cap Row {i}")
    await db.commit()

    db.add(SystemSetting(key="search_export_max_rows", value=2))
    await db.commit()
    settings_store.invalidate("search_export_max_rows")
    try:
        async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
            resp = await client.post(
                f"{API}/search/exports", json={"kind": "applications", "format": "xlsx"}
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["row_count"] == 2, "the configured cap was not enforced"
            assert body["total_matched"] >= 3, (
                "a truncated export must report how much it matched — "
                "the whole point is that truncation stays VISIBLE, never silent"
            )
    finally:
        await db.execute(text("DELETE FROM system_settings WHERE key = 'search_export_max_rows'"))
        await db.commit()
        settings_store.invalidate("search_export_max_rows")


async def test_the_watermark_carries_the_operators_full_name_and_the_date_pdf(
    db: AsyncSession, leshoz: Organization
):
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Watermark Case PDF")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "pdf"}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        user = await db.get(User, uuid.UUID(body["user_id"]))
        assert user is not None

        file_resp = await client.get(f"{API}/search/exports/{body['id']}/file")
        assert file_resp.status_code == 200
        assert file_resp.headers["content-type"] == "application/pdf"
        assert file_resp.content.startswith(b"%PDF")

        text_content = "\n".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(file_resp.content)).pages
        )
        assert user.full_name in text_content, "the operator's name did not survive into the file"
        assert business_today().isoformat() in text_content, (
            "the date did not survive into the file"
        )


async def test_the_watermark_carries_the_operators_full_name_and_the_date_xlsx(
    db: AsyncSession, leshoz: Organization
):
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Watermark Case XLSX")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "xlsx"}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        user = await db.get(User, uuid.UUID(body["user_id"]))
        assert user is not None

        file_resp = await client.get(f"{API}/search/exports/{body['id']}/file")
        assert file_resp.status_code == 200
        assert file_resp.headers["content-type"].startswith("application/vnd.openxmlformats")

        import openpyxl

        sheet = openpyxl.load_workbook(io.BytesIO(file_resp.content)).active
        assert sheet is not None
        watermark_cell = sheet.cell(row=1, column=1).value
        assert isinstance(watermark_cell, str)
        assert user.full_name in watermark_cell
        assert business_today().isoformat() in watermark_cell


async def test_one_audit_row_is_written_per_export(db: AsyncSession, leshoz: Organization):
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Audit Row Case")
    await db.commit()

    job_id: uuid.UUID | None = None
    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "xlsx"}
        )
        assert resp.status_code == 201, resp.text
        job_id = uuid.UUID(resp.json()["id"])
    assert job_id is not None

    rows = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "search.export", AuditLog.object_id == job_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, "exactly one audit row must be written per export, not zero, not two"


async def test_a_zone_scoped_operators_export_contains_only_their_own_zones_rows(
    db: AsyncSession, leshoz: Organization, other_leshoz: Organization
):
    """The test this whole feature exists to pass (track brief, verbatim):
    an export that widened what a role can see on screen would be the worst
    possible defect a С22 track could ship."""
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Belongs To My Zone")
    await make_application(
        db, org=other_leshoz, status="CLOSED", applicant_name="Belongs To Another Zone"
    )
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "pdf"}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["row_count"] == 1, "a zone-scoped export must not see another zone's rows"

        file_resp = await client.get(f"{API}/search/exports/{body['id']}/file")
        text_content = "\n".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(file_resp.content)).pages
        )
        assert "Belongs To My Zone" in text_content
        assert "Belongs To Another Zone" not in text_content, (
            "search export leaked another organization's application"
        )


async def test_an_export_is_visible_only_to_the_operator_who_ran_it(
    db: AsyncSession, leshoz: Organization
):
    await make_application(db, org=leshoz, status="CLOSED", applicant_name="Owner Only Case")
    await db.commit()

    job_id: str | None = None
    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await client.post(
            f"{API}/search/exports", json={"kind": "applications", "format": "xlsx"}
        )
        job_id = resp.json()["id"]
    assert job_id is not None

    async for other_client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        resp = await other_client.get(f"{API}/search/exports/{job_id}")
        assert resp.status_code == 404, "another operator must not read someone else's export job"
        resp = await other_client.get(f"{API}/search/exports/{job_id}/file")
        assert resp.status_code == 404
