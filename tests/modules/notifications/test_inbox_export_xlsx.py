"""Stage 13: `GET /notifications/export.xlsx` — the caller's own inbox on
paper. Same scope (own rows only, ruling R2), same `unread` filter, readable
cells, the id last."""

from app.main import create_app
from app.modules.notifications import service
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"
EXPORT = f"{API}/notifications/export.xlsx"
EVENT = "permit.active"  # keeps an active `sms` template after 0060 (ruling #211)


async def _signed_in(db, **overrides):
    user = await make_user(db, **overrides)
    _, token, csrf = await make_session(db, user)
    return user, token, csrf


async def test_the_export_mirrors_the_inbox(db):
    """Own rows only, the `unread` filter, the status transition and the
    read state in their cells, and the cap — one inbox of three rows."""
    mine, token, csrf = await _signed_in(db)
    other = await make_user(db)
    (read_row,) = await service.notify(
        db, event_code=EVENT, recipient_user_id=mine.id, params={"permit_number": "P-EXP-1"}
    )
    await service.notify(
        db,
        event_code="application.status_changed",
        recipient_user_id=mine.id,
        params={"status_from": "SUBMITTED", "status_to": "APPROVED"},
    )
    await service.notify(
        db, event_code=EVENT, recipient_user_id=other.id, params={"permit_number": "P-EXP-2"}
    )
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/notifications", params={"page_size": 100})
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(EXPORT, params={"lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Код события" and headers[-1] == "ID"
        assert {str(row[-1]) for row in rows} == listed_ids
        texts = [str(row[2]) for row in rows]
        assert any("P-EXP-1" in t for t in texts)
        assert not any("P-EXP-2" in t for t in texts)  # another user's own row never leaks

        # The cells: the transition's two statuses, no label map on the code,
        # and `read_at` empty while unread.
        row = next(r for r in rows if r[0] == "application.status_changed")
        assert row[3] == "SUBMITTED"  # status_from
        assert row[4] == "APPROVED"  # status_to
        assert row[8] is None  # read_at, never read

        # The `unread` filter drops the row once read.
        await client.post(f"{API}/notifications/{read_row.id}/read")
        resp = await client.get(EXPORT, params={"unread": True})
        assert resp.status_code == 200
        assert str(read_row.id) not in {str(row[-1]) for row in xlsx_rows(resp.content)[1]}

        with export_cap(1):  # two rows of mine, one fits
            resp = await client.get(EXPORT)
            assert resp.headers["x-export-total"] == "2"
            assert_export_cut(resp, cap=1)


async def test_export_requires_authentication(db):
    async with make_client(create_app(), lifespan=True) as client:
        resp = await client.get(EXPORT)
    assert resp.status_code == 401
