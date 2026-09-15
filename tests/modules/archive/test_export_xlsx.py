"""Stage 13: `GET /archive/export.xlsx` — the archive register on paper.
Same zone, same filters, readable cells, the id last."""

from app.modules.archive.permissions import ARCHIVE_MANAGE, ARCHIVE_VIEW
from tests.conftest import assert_export_cut, export_cap, xlsx_rows
from tests.modules.archive.conftest import _client_for, _client_with_role, make_application

EXPORT = "/api/v1/archive/export.xlsx"


async def test_the_export_mirrors_the_list(db, leshoz, other_leshoz):
    """One register, one file: the zone the list applies, the filter it
    takes, labels rather than codes in the cells, and the cap with its
    three headers — all against the same rows, built once."""
    mine = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    mine_too = await make_application(db, org=leshoz, status="CLOSED", applicant_name="C C")
    theirs = await make_application(db, org=other_leshoz, status="CLOSED", applicant_name="B B")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        for application in (mine, mine_too):
            archived = await client.post(f"/api/v1/archive/application/{application.id}", json={})
            assert archived.status_code == 200
    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=other_leshoz.id):
        archived = await client.post(f"/api/v1/archive/application/{theirs.id}", json={})
        assert archived.status_code == 200

    async for client in _client_for(db, ARCHIVE_VIEW, organization_id=leshoz.id):
        listed = await client.get("/api/v1/archive", params={"page_size": 100})
        listed_ids = {row["object_id"] for row in listed.json()["items"]}
        assert str(theirs.id) not in listed_ids  # the zone hides it from the screen

        # The same rows as the list, the id last, the sheet's language honoured.
        resp = await client.get(EXPORT, params={"lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Тип объекта" and headers[-1] == "ID"
        assert {str(row[1]) for row in rows} == listed_ids

        # Labels, not codes.
        _, rows = xlsx_rows((await client.get(EXPORT, params={"lang": "uz_latn"})).content)
        row = next(r for r in rows if r[1] == str(mine.id))
        assert row[0] == "Ariza"  # the object_type LABEL, not "application"
        assert row[3] == "Saqlangan"  # the status LABEL, not "stored"

        # The list's own filter.
        resp = await client.get(EXPORT, params={"object_type": "permit", "lang": "uz_latn"})
        assert resp.status_code == 200
        assert xlsx_rows(resp.content)[1] == []

        # The cap: two rows of ours are in the register, one fits.
        with export_cap(1):
            assert_export_cut(await client.get(EXPORT), cap=1)


async def test_export_requires_the_permission(db, leshoz):
    async for client in _client_with_role(db, "inspector"):
        resp = await client.get(EXPORT)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"
