"""Stage 13: `/help/tickets/export.xlsx` and `/admin/help/faq/export.xlsx`
are their lists on paper — same scope/permission, same filters, readable
cells, the id last."""

import pytest

from app.main import create_app
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.auth.test_sessions import make_session, make_user
from tests.modules.help.conftest import auth_client, faq_manager

pytestmark = pytest.mark.asyncio

API = "/api/v1"
TICKETS = f"{API}/help/tickets/export.xlsx"
FAQ = f"{API}/admin/help/faq/export.xlsx"


def _ids(rows) -> set[str]:
    """The last column of every data row, as strings — a cell is typed as a
    broad union openpyxl itself does not guarantee hashable, so every id is
    coerced through `str()` before it enters a set (every id column ever
    holds one already, via `xlsx.id_column`)."""
    return {str(row[-1]) for row in rows}


def _row_by_id(rows, id_: str):
    """The FAQ catalogue has no per-caller scope (`help.faq.manage` sees
    every row, published or not), so the shared, persistent test DB can
    hold rows from earlier runs — `rows[0]` would read whichever one happens
    to sort first, not necessarily this fixture's own (lesson: filter on
    something fresh per fixture, never assume order)."""
    for row in rows:
        if str(row[-1]) == id_:
            return row
    raise AssertionError(f"no row with id {id_!r} in the sheet")


async def _signed_in(db, **overrides):
    user = await make_user(db, **overrides)
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return user, token, csrf


# --- Tickets -----------------------------------------------------------


async def test_the_tickets_export_mirrors_the_list(db):
    """The caller's own tickets as the list gives them, the list's status
    filter, labels rather than codes with the number first, and the cap."""
    owner, token, csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(f"{API}/help/tickets", json={"subject": "Zebra", "body": "b"})
        assert created.status_code == 201, created.text
        ticket_id = created.json()["id"]
        second = await client.post(f"{API}/help/tickets", json={"subject": "Capped", "body": "b"})
        assert second.status_code == 201, second.text

        listed = await client.get(f"{API}/help/tickets", params={"page_size": 100})
        listed_ids = {row["id"] for row in listed.json()["items"]}
        assert ticket_id in listed_ids

        resp = await client.get(TICKETS, params={"lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Номер" and headers[-1] == "ID"
        assert _ids(rows) == listed_ids

        _, rows = xlsx_rows((await client.get(TICKETS, params={"lang": "uz_latn"})).content)
        row = _row_by_id(rows, ticket_id)
        assert row[0] == created.json()["number"]  # the human number comes first
        assert row[1] == "Zebra"
        assert row[2] == "Yangi"  # status label ("new"), not the raw code

        resp = await client.get(TICKETS, params={"status": "closed", "lang": "uz_latn"})
        assert resp.status_code == 200
        assert xlsx_rows(resp.content)[1] == []

        with export_cap(1):  # two tickets of the owner's, one fits
            resp = await client.get(TICKETS)
            assert resp.headers["x-export-total"] == "2"
            assert_export_cut(resp, cap=1)


async def test_tickets_export_is_empty_not_403_for_a_caller_with_no_scope(db):
    owner, owner_token, owner_csrf = await _signed_in(db)
    stranger, stranger_token, stranger_csrf = await _signed_in(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, owner_token, owner_csrf)
        created = await client.post(
            f"{API}/help/tickets", json={"subject": "Owner's own", "body": "b"}
        )
        assert created.status_code == 201, created.text

        auth_client(client, stranger_token, stranger_csrf)
        resp = await client.get(TICKETS)
    assert resp.status_code == 200
    # `stranger` has never opened or been assigned a ticket — an empty 200,
    # the same shape the list gives a caller in their position, never a 403.
    assert xlsx_rows(resp.content)[1] == []


# --- FAQ (admin) -----------------------------------------------------------


async def test_the_faq_export_mirrors_the_list(db):
    """Every row the manager's list holds, the list's status filter, the
    localized question and the status label in the cells, and the cap."""
    _, token, csrf = await faq_manager(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/help/faq",
            json={
                "question": {"uz_cyrl": "Нархими?", "uz_latn": "Narxi qancha?"},
                "answer": {"uz_cyrl": "Ж.", "uz_latn": "J."},
                "category": "billing",
                "sort_order": 3,
            },
        )
        assert created.status_code == 201, created.text  # status defaults to "draft"
        faq_id = created.json()["id"]
        published = await client.patch(
            f"{API}/admin/help/faq/{faq_id}", json={"status": "published"}
        )
        assert published.status_code == 200, published.text
        draft = await client.post(
            f"{API}/admin/help/faq",
            json={
                "question": {"uz_cyrl": "Q?", "uz_latn": "Q?"},
                "answer": {"uz_cyrl": "A.", "uz_latn": "A."},
            },
        )
        assert draft.status_code == 201, draft.text

        listed = await client.get(f"{API}/admin/help/faq")
        listed_ids = {row["id"] for row in listed.json()}
        assert faq_id in listed_ids

        resp = await client.get(FAQ, params={"lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Вопрос" and headers[-1] == "ID"
        assert _ids(rows) == listed_ids

        _, rows = xlsx_rows((await client.get(FAQ, params={"lang": "uz_latn"})).content)
        row = _row_by_id(rows, faq_id)
        assert row[0] == "Narxi qancha?"  # localized question, not the raw dict
        assert row[1] == "billing"
        assert row[2] == "Chop etilgan"  # status label ("published"), not the raw code

        resp = await client.get(FAQ, params={"status": "archived", "lang": "uz_latn"})
        assert resp.status_code == 200
        assert xlsx_rows(resp.content)[1] == []

        with export_cap(1):  # the published row and the draft at least, one fits
            assert_export_cut(await client.get(FAQ), cap=1)


async def test_faq_export_requires_the_same_permission_as_the_list(db):
    stranger = await make_user(db)
    _, token, csrf = await make_session(db, stranger)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        list_resp = await client.get(f"{API}/admin/help/faq")
        export_resp = await client.get(FAQ)
    assert list_resp.status_code == 403
    assert export_resp.status_code == 403
    assert export_resp.json()["error"]["code"] == "ERR-ACL-001"
