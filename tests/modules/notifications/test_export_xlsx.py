"""Stage 13 (ruling #204): the notification template register on paper — the
same filters, the same permission gate, readable cells, never the body."""

import uuid

import pytest

from app.main import create_app
from app.modules.notifications.permissions import TEMPLATES_MANAGE
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

pytestmark = pytest.mark.asyncio

API = "/api/v1/admin/notification-templates"
EXPORT = f"{API}/export.xlsx"


def _payload(event_code: str, channel: str = "inapp", **overrides) -> dict:
    body = {
        "event_code": event_code,
        "channel": channel,
        "body": {"uz_cyrl": "Матн {x}", "uz_latn": "Matn {x}", "ru": "Текст {x}"},
    }
    body.update(overrides)
    return body


def _exported_ids(content: bytes) -> set[str]:
    return {str(row[-1]) for row in xlsx_rows(content)[1]}


async def test_the_export_mirrors_the_list(db):
    """The same rows under the same filters, labels rather than codes, the
    body never in a cell, and the cap — one signed-in manager, one code."""
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.export{uuid.uuid4().hex[:8]}"
    secret_body = "SECRET-BODY-TEXT-SHOULD-NOT-APPEAR"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        email = await client.post(
            API,
            json=_payload(
                code,
                channel="email",
                subject={"uz_latn": "Mavzu matni"},
                body={"uz_latn": secret_body, "uz_cyrl": "x", "ru": "x"},
            ),
        )
        sms = await client.post(API, json=_payload(code, channel="sms"))
        assert email.status_code == 201 and sms.status_code == 201, (email.text, sms.text)

        listed = await client.get(API, params={"event_code": code})
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}
        resp = await client.get(EXPORT, params={"event_code": code, "lang": "ru"})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert _exported_ids(resp.content) == listed_ids == {email.json()["id"], sms.json()["id"]}

        # The list's channel filter, and the cells of the row it keeps.
        resp = await client.get(
            EXPORT, params={"event_code": code, "channel": "email", "lang": "uz_latn"}
        )
        assert resp.status_code == 200, resp.text
        _, rows = xlsx_rows(resp.content)
        assert {str(row[-1]) for row in rows} == {email.json()["id"]}
        (row,) = rows
        assert row[0] == code  # event code first
        assert row[1] == "Email"  # channel label
        assert row[2] == "Amaldagi"  # status label, not "active"
        assert row[4] == "Mavzu matni"  # subject, localized
        assert secret_body not in "".join(str(v) for v in row if v is not None)

        with export_cap(1):  # two templates under this code, one fits
            resp = await client.get(EXPORT, params={"event_code": code})
            assert resp.headers["x-export-total"] == "2"
            assert_export_cut(resp, cap=1)


async def test_export_without_the_permission_matches_the_list_status(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(API)
        resp = await client.get(EXPORT)
    assert listed.status_code == 403
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == listed.json()["error"]["code"]
