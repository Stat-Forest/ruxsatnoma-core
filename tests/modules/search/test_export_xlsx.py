"""Stage 13: `GET /search/export.xlsx` — the plain register export beside
the prosecutor's watermarked `POST /search/exports` (untouched). The export
is the list on paper — same zone, same filters, readable cells, the id
last."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization
from app.modules.search.permissions import SEARCH_USE
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.search.conftest import _client_for, _client_with_role, make_application

EXPORT = "/api/v1/search/export.xlsx"


async def test_the_export_mirrors_the_list(
    db: AsyncSession, leshoz: Organization, other_leshoz: Organization
):
    """The zone the list applies, the status filter it takes, labels rather
    than codes with the number first, and the cap — one set of rows."""
    closed = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    cancelled = await make_application(db, org=leshoz, status="CANCELLED", applicant_name="C C")
    theirs = await make_application(db, org=other_leshoz, status="CLOSED", applicant_name="B B")
    await db.commit()

    async for client in _client_for(db, SEARCH_USE, organization_id=leshoz.id):
        listed = await client.get(
            "/api/v1/search", params={"kind": "applications", "page_size": 100}
        )
        listed_ids = {row["id"] for row in listed.json()["items"]}
        assert str(theirs.id) not in listed_ids  # the zone hides it from the screen

        resp = await client.get(EXPORT, params={"kind": "applications", "lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Номер" and headers[-1] == "ID"
        exported_ids = {str(row[-1]) for row in rows}
        assert str(closed.id) in exported_ids
        assert exported_ids == listed_ids

        # The list's status filter, and the cells of the row it keeps.
        resp = await client.get(
            EXPORT, params={"kind": "applications", "status": "CLOSED", "lang": "uz_latn"}
        )
        assert resp.status_code == 200
        _, rows = xlsx_rows(resp.content)
        exported_ids = {str(row[-1]) for row in rows}
        assert str(closed.id) in exported_ids
        assert str(cancelled.id) not in exported_ids
        row = next(r for r in rows if r[-1] == str(closed.id))
        assert row[0] == closed.number  # the human number first
        assert row[1] == "Ariza"  # kind label, not the raw code
        assert row[4] == "Yopilgan"  # the status LABEL, not CLOSED

        with export_cap(1):  # two rows of ours in the zone, one fits
            assert_export_cut(await client.get(EXPORT, params={"kind": "applications"}), cap=1)


async def test_export_requires_the_permission(db: AsyncSession, leshoz: Organization):
    async for client in _client_with_role(db, "inspector"):  # search.use NOT granted
        resp = await client.get(EXPORT, params={"kind": "applications"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"
