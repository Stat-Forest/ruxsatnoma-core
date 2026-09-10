"""Stage 13 (ruling #204): the admin module's registers on paper — users,
organizations, announcements, legal documents, and the integrations
outbox/DLQ. Same scope, same filters, readable cells, the id last."""

import io

import pytest
from openpyxl import load_workbook

from app.core import settings_store
from app.main import create_app
from app.modules.auth.permissions import USERS_MANAGE, USERS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401

pytestmark = pytest.mark.asyncio

API = "/api/v1"
LEGAL_DOCUMENT_BODY = {
    "title": {"uz_latn": "Test hujjat", "ru": "Тестовый акт"},
    "doc_number": "TEST-1",
    "adopted_on": "2020-01-01",
    "source_url": "https://lex.uz/docs/test",
}


def _sheet(content: bytes):
    sheet = load_workbook(io.BytesIO(content)).active
    assert sheet is not None  # a workbook this module wrote always has one
    return sheet


def _exported_ids(content: bytes) -> set[str]:
    return {str(row[-1]) for row in _sheet(content).iter_rows(min_row=2, values_only=True)}


# ---------------------------------------------------------------------------
# Users — GET /admin/users/export.xlsx
# ---------------------------------------------------------------------------


async def test_users_export_holds_exactly_the_rows_the_list_shows(db, leshoz):  # noqa: F811
    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db, organization_id=leshoz.id)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/admin/users", params={"q": target.login})
        resp = await client.get(
            f"{API}/admin/users/export.xlsx", params={"q": target.login, "lang": "ru"}
        )
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()["items"]}
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["x-export-truncated"] == "false"
    exported_ids = _exported_ids(resp.content)
    assert exported_ids == listed_ids
    assert exported_ids == {str(target.id)}


async def test_users_export_applies_the_same_filters_as_the_list(db, leshoz, other_leshoz):  # noqa: F811
    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    in_org = await make_user(db, organization_id=leshoz.id)
    await make_user(db, organization_id=other_leshoz.id)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        resp = await client.get(
            f"{API}/admin/users/export.xlsx", params={"organization_id": str(leshoz.id)}
        )
    assert resp.status_code == 200, resp.text
    exported_ids = _exported_ids(resp.content)
    assert str(in_org.id) in exported_ids


async def test_users_export_renders_labels_not_codes(db, leshoz):  # noqa: F811
    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db, organization_id=leshoz.id)
    target.full_name = "Aaaaa Export Target"
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        resp = await client.get(
            f"{API}/admin/users/export.xlsx", params={"q": target.login, "lang": "uz_latn"}
        )
    assert resp.status_code == 200, resp.text
    row = next(_sheet(resp.content).iter_rows(min_row=2, values_only=True))
    assert row[0] == "Aaaaa Export Target"  # F.I.Sh. first
    assert row[6] == "Faol"  # status label, not "active"


async def test_users_export_truncates_at_the_cap_and_says_so(db, monkeypatch):
    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await make_user(db)
    await make_user(db)
    await db.commit()

    real_get_int = settings_store.get_int

    async def one(_db, key):
        if key == "register_export_max_rows":
            return 1
        return await real_get_int(_db, key)

    monkeypatch.setattr(settings_store, "get_int", one)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        resp = await client.get(f"{API}/admin/users/export.xlsx")
    assert resp.status_code == 200, resp.text
    total = int(resp.headers["x-export-total"])
    assert total >= 2
    assert resp.headers["x-export-truncated"] == "true"
    assert resp.headers["x-export-rows"] == "1"
    assert len(list(_sheet(resp.content).iter_rows(min_row=2, values_only=True))) == 1


async def test_users_export_without_the_permission_matches_the_list_status(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/admin/users")
        resp = await client.get(f"{API}/admin/users/export.xlsx")
    assert listed.status_code == 403
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == listed.json()["error"]["code"]


async def test_users_export_rejects_an_unknown_language(db):
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        resp = await client.get(f"{API}/admin/users/export.xlsx", params={"lang": "en"})
    assert resp.status_code == 422
