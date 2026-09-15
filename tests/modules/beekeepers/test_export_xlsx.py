"""Stage 13: `GET /beekeepers/export.xlsx` — the beekeepers register on
paper. `beekeepers` is a GLOBAL, unscoped catalog (ruling #182 — no zone at
all), so — like `report_forms` — its total can already exceed the list's
own `page_size<=100` ceiling on this shared, persistent test DB (lesson:
assert on something fresh, never an assumed-small or empty neighbourhood).
Compared on TOTAL count plus this test's own fresh row's presence, never on
full id-set equality against one capped list page."""

import uuid

from app.main import create_app
from app.modules.auth.models import UserPermission
from app.modules.beekeepers.permissions import BEEKEEPERS_MANAGE
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.auth.test_sessions import make_session, make_user

EXPORT = "/api/v1/beekeepers/export.xlsx"


def _unique_pinfl() -> str:
    # Leading digit 4 — the digit block this package's own test_router.py
    # already claims on this shared, persistent test DB.
    return f"4{uuid.uuid4().int % 10**13:013d}"


def _auth(client, token: str, csrf: str):
    client.cookies.set("session", token)
    client.cookies.set("csrf_token", csrf)
    client.headers["X-CSRF-Token"] = csrf
    return client


async def _registrar_client(db, *codes: str):
    user = await make_user(db, role_code="executor_staff")
    for code in codes:
        db.add(UserPermission(user_id=user.id, permission_code=code))
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    return make_client(app, lifespan=True), token, csrf


async def _create_beekeeper(client, *, certificate_no: str, full_name: str) -> str:
    resp = await client.post(
        "/api/v1/beekeepers",
        json={
            "certificate_no": certificate_no,
            "pinfl": _unique_pinfl(),
            "passport_series": "AB",
            "passport_number": "1234567",
            "full_name": full_name,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_the_export_mirrors_the_list(db):
    """The list's total and its `q` filter, labels rather than codes, no
    passport and no STIR in the file (the screen shows neither; a bulk file
    must not widen what it shows), and the cap — two fresh rows."""
    ctx, token, csrf = await _registrar_client(db, BEEKEEPERS_MANAGE)
    async with ctx as client:
        _auth(client, token, csrf)
        certificate_no = f"EXP-{uuid.uuid4().hex[:8].upper()}"
        beekeeper_id = await _create_beekeeper(
            client, certificate_no=certificate_no, full_name="Export Test Beekeeper"
        )
        await _create_beekeeper(
            client, certificate_no=f"CAP-{uuid.uuid4().hex[:8].upper()}", full_name="Cap"
        )

        listed_total = (await client.get("/api/v1/beekeepers", params={"page_size": 1})).json()[
            "total"
        ]

        resp = await client.get(EXPORT, params={"lang": "ru"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert int(resp.headers["x-export-total"]) == listed_total
        headers, rows = xlsx_rows(resp.content)
        assert headers[0] == "Номер сертификата" and headers[-1] == "ID"
        assert beekeeper_id in {str(row[-1]) for row in rows}
        assert "Серия паспорта" not in headers and "Номер паспорта" not in headers
        assert "СТИР" not in headers
        assert "ПИНФЛ" in headers  # a screen column, it stays

        # The list's `q` filter, and the cells of the row it keeps.
        resp = await client.get(EXPORT, params={"q": certificate_no, "lang": "uz_latn"})
        assert resp.status_code == 200
        _, rows = xlsx_rows(resp.content)
        assert {str(row[-1]) for row in rows} == {beekeeper_id}
        (row,) = rows
        assert row[0] == certificate_no  # the human number first
        assert (
            row[5] == "Faol"
        )  # the status LABEL, not "active" (ruling #217 put the term before it)

        with export_cap(1):
            assert_export_cut(await client.get(EXPORT), cap=1)


async def test_export_requires_the_permission(db):
    ctx, token, csrf = await _registrar_client(db)  # no grants
    async with ctx as client:
        _auth(client, token, csrf)
        resp = await client.get(EXPORT)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"
