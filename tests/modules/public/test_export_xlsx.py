"""Stage 13: `GET /admin/public/appeals/export.xlsx` is the staff triage
list on paper — same `status` filter, same `public.appeals.manage` gate,
readable cells, the id last.

Fixtures go through the real transition (`POST /public/appeals` then
`POST .../status` / `.../answer`), the same pattern `test_appeals.py`
already uses, never by assigning `CitizenAppeal.status` by hand."""

import pytest

from app.main import create_app
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.public.conftest import appeals_manager, auth_client

pytestmark = pytest.mark.asyncio

API = "/api/v1"
EXPORT = f"{API}/admin/public/appeals/export.xlsx"


async def _submit(client, *, subject: str, phone: str = "+998901234567") -> str:
    r = await client.post(
        f"{API}/public/appeals",
        json={
            "applicant_name": "Test Citizen",
            "contact": {"phone": phone, "email": "citizen@example.uz"},
            "subject": subject,
            "body": "Body text",
        },
    )
    assert r.status_code == 201
    return r.json()["number"]


async def test_the_export_mirrors_the_list(db):
    """The same rows as the list under the same `status` filter, labels
    rather than codes, a row that changes status moves between the two
    files exactly as between the two screens, and the cap."""
    _, token, csrf = await appeals_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        number = await _submit(client, subject="Export test - labels", phone="+998907654321")
        to_close = await _submit(client, subject="Export test - to be closed")

        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/admin/public/appeals", params={"status": "new"})
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert listed_ids

        resp = await client.get(EXPORT, params={"status": "new", "lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Номер" and headers[-1] == "ID"
        assert {str(row[-1]) for row in rows} == listed_ids

        # Labels, not codes; the contact's phone in its own cell.
        _, rows = xlsx_rows(
            (await client.get(EXPORT, params={"status": "new", "lang": "uz_latn"})).content
        )
        row = next(r for r in rows if r[0] == number)
        assert row[2] == "Yangi"  # the status label, not "new"
        assert row[4] == "+998907654321"  # phone, from `contact`

        # The list's filter follows the row: closed leaves "new" and enters "closed".
        appeal = next(item for item in listed.json()["items"] if item["number"] == to_close)
        advance = await client.post(
            f"{API}/admin/public/appeals/{appeal['id']}/status", json={"to_status": "closed"}
        )
        assert advance.status_code == 200
        _, rows_new = xlsx_rows((await client.get(EXPORT, params={"status": "new"})).content)
        assert appeal["id"] not in {str(row[-1]) for row in rows_new}
        _, rows_closed = xlsx_rows((await client.get(EXPORT, params={"status": "closed"})).content)
        assert appeal["id"] in {str(row[-1]) for row in rows_closed}

        # The cap: `number` and whatever earlier runs left in "new" — at
        # least one row, cut to one.
        await _submit(client, subject="Cap test")
        with export_cap(1):
            resp = await client.get(EXPORT, params={"status": "new"})
            assert int(resp.headers["x-export-total"]) > 1
            assert_export_cut(resp, cap=1)


async def test_export_gets_the_same_403_the_list_gives_without_the_permission(db):
    other = await make_user(db)
    _, other_token, other_csrf = await make_session(db, other)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, other_token, other_csrf)
        listed = await client.get(f"{API}/admin/public/appeals")
        exported = await client.get(EXPORT)
    assert listed.status_code == exported.status_code == 403
    assert exported.json()["error"]["code"] == "ERR-ACL-001"
