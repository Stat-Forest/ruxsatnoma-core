"""Stage 13 (ruling #204): the admin module's registers on paper — users,
organizations, announcements, legal documents, and the integrations
outbox/DLQ. Same scope, same filters, readable cells, the id last.

One "mirrors the list" test per register — the same rows as the list under
the same filter, labels rather than codes, the list's other filter, the cap —
plus the register's own permission shape."""

import uuid

import pytest

from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.admin.permissions import (
    ANNOUNCEMENTS_MANAGE,
    INTEGRATIONS_MANAGE,
    INTEGRATIONS_VIEW,
    LEGAL_DOCUMENTS_MANAGE,
)
from app.modules.auth.permissions import USERS_MANAGE
from app.modules.integrations import senders
from app.modules.integrations import service as integrations_service
from app.modules.integrations.service import discard_dead_letter, record_dead_letter
from tests.conftest import assert_export_cut, export_cap, make_client, xlsx_rows
from tests.modules.admin.test_announcements import make_announcement
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401

pytestmark = pytest.mark.asyncio

API = "/api/v1"
USERS = f"{API}/admin/users/export.xlsx"
ORGANIZATIONS = f"{API}/refs/organizations/export.xlsx"
ANNOUNCEMENTS = f"{API}/admin/announcements/export.xlsx"
LEGAL_DOCUMENTS = f"{API}/admin/legal-documents/export.xlsx"
OUTBOX = f"{API}/admin/integrations/outbox/export.xlsx"
DEAD_LETTERS = f"{API}/admin/integrations/dead-letters/export.xlsx"
LEGAL_DOCUMENT_BODY = {
    "title": {"uz_latn": "Test hujjat", "ru": "Тестовый акт"},
    "doc_number": "TEST-1",
    "adopted_on": "2020-01-01",
    "source_url": "https://lex.uz/docs/test",
}


def _exported_ids(content: bytes) -> set[str]:
    return {str(row[-1]) for row in xlsx_rows(content)[1]}


def _row(content: bytes, id_: str):
    return next(r for r in xlsx_rows(content)[1] if str(r[-1]) == id_)


async def _same_status_without_the_permission(db, list_path: str, export_path: str) -> None:
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(list_path)
        resp = await client.get(export_path)
    assert listed.status_code == 403
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == listed.json()["error"]["code"]


# ---------------------------------------------------------------------------
# Users — GET /admin/users/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_users_export_mirrors_the_list(db, leshoz, other_leshoz):  # noqa: F811
    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db, organization_id=leshoz.id)
    target.full_name = "Aaaaa Export Target"
    await make_user(db, organization_id=other_leshoz.id)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/admin/users", params={"q": target.login})
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(USERS, params={"q": target.login, "lang": "ru"})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert resp.headers["x-export-truncated"] == "false"
        assert _exported_ids(resp.content) == listed_ids == {str(target.id)}

        resp = await client.get(USERS, params={"q": target.login, "lang": "uz_latn"})
        row = _row(resp.content, str(target.id))
        assert row[0] == "Aaaaa Export Target"  # F.I.Sh. first
        assert row[6] == "Faol"  # status label, not "active"

        resp = await client.get(USERS, params={"organization_id": str(leshoz.id)})
        assert resp.status_code == 200, resp.text
        assert str(target.id) in _exported_ids(resp.content)

        with export_cap(1):  # the manager and the two users made here at least
            resp = await client.get(USERS)
            assert int(resp.headers["x-export-total"]) >= 2
            assert_export_cut(resp, cap=1)


async def test_users_export_without_the_permission_matches_the_list_status(db):
    await _same_status_without_the_permission(db, f"{API}/admin/users", USERS)


# ---------------------------------------------------------------------------
# Organizations — GET /refs/organizations/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_organizations_export_mirrors_the_list(db, agency):
    _, token, csrf = await signed_in_with(db)  # authenticated, no permission needed (ruling 10)
    suffix = uuid.uuid4().hex[:8]
    parent = Organization(
        kind="territorial", code=f"parent-{suffix}", name={"uz_cyrl": "Тест"}, parent_id=agency.id
    )
    db.add(parent)
    await db.flush()
    child = Organization(
        kind="leshoz",
        code=f"child-{suffix}",
        name={"uz_latn": "A Test Leshoz"},
        parent_id=parent.id,
        status="active",
    )
    archived_child = Organization(
        kind="leshoz",
        code=f"archived-{suffix}",
        name={"uz_cyrl": "Архив"},
        parent_id=parent.id,
        status="archived",
    )
    db.add_all([child, archived_child])
    await db.commit()
    under_parent = {"parent_id": str(parent.id)}

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(f"{API}/refs/organizations", params=under_parent)
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(ORGANIZATIONS, params={**under_parent, "lang": "ru"})
        assert resp.status_code == 200, resp.text
        assert _exported_ids(resp.content) == listed_ids
        assert str(child.id) in listed_ids

        resp = await client.get(ORGANIZATIONS, params={**under_parent, "lang": "uz_latn"})
        row = _row(resp.content, str(child.id))
        assert row[0] == "A Test Leshoz"  # name first
        assert row[2] == "Oʻrmon xoʻjaligi"  # kind label, not "leshoz"

        resp = await client.get(ORGANIZATIONS, params={**under_parent, "status": "archived"})
        assert resp.status_code == 200, resp.text
        assert _exported_ids(resp.content) == {str(archived_child.id)}

        with export_cap(1):
            assert_export_cut(await client.get(ORGANIZATIONS), cap=1)


async def test_organizations_export_without_authentication_matches_the_list_status(db):
    async with make_client(create_app(), lifespan=True) as client:
        listed = await client.get(f"{API}/refs/organizations")
        resp = await client.get(ORGANIZATIONS)
    assert listed.status_code == 401
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == listed.json()["error"]["code"]


# ---------------------------------------------------------------------------
# Announcements — GET /admin/announcements/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_announcements_export_mirrors_the_list(db):
    manager, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    marker = uuid.uuid4().hex[:8]
    draft = await make_announcement(
        db,
        created_by=manager.id,
        status="draft",
        title={"uz_latn": f"Export title {marker}"},
        audience=None,
    )
    another_draft = await make_announcement(db, created_by=manager.id, status="draft")
    published = await make_announcement(
        db, created_by=manager.id, status="published", title={"uz_cyrl": f"П{marker}"}
    )
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(
            f"{API}/admin/announcements", params={"status": "draft", "page_size": 100}
        )
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(ANNOUNCEMENTS, params={"status": "draft", "lang": "ru"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert exported_ids == listed_ids
        assert {str(draft.id), str(another_draft.id)} <= exported_ids

        resp = await client.get(ANNOUNCEMENTS, params={"status": "draft", "lang": "uz_latn"})
        row = _row(resp.content, str(draft.id))
        assert row[0] == f"Export title {marker}"
        assert row[1] == "Qoralama"  # status label, not "draft"
        assert row[2] == "Barcha foydalanuvchilar"  # no audience -> everyone

        resp = await client.get(ANNOUNCEMENTS, params={"status": "published"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert str(published.id) in exported_ids
        assert str(draft.id) not in exported_ids

        with export_cap(1):  # two drafts at least, one fits
            resp = await client.get(ANNOUNCEMENTS, params={"status": "draft"})
            assert int(resp.headers["x-export-total"]) >= 2
            assert_export_cut(resp, cap=1)


async def test_announcements_export_without_the_permission_matches_the_list_status(db):
    await _same_status_without_the_permission(db, f"{API}/admin/announcements", ANNOUNCEMENTS)


# ---------------------------------------------------------------------------
# Legal documents — GET /admin/legal-documents/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_legal_documents_export_mirrors_the_list(db):
    _, token, csrf = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    await db.commit()
    marker = uuid.uuid4().hex[:8]
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/legal-documents",
            json={**LEGAL_DOCUMENT_BODY, "doc_number": f"LBL-{marker}"},
        )
        assert created.status_code == 201, created.text
        doc_id = created.json()["id"]
        second = await client.post(
            f"{API}/admin/legal-documents",
            json={**LEGAL_DOCUMENT_BODY, "doc_number": f"CAP-{marker}"},
        )
        assert second.status_code == 201, second.text
        to_publish = await client.post(
            f"{API}/admin/legal-documents",
            json={**LEGAL_DOCUMENT_BODY, "doc_number": f"PUB-{marker}"},
        )
        published_id = to_publish.json()["id"]
        published = await client.post(f"{API}/admin/legal-documents/{published_id}/publish")
        assert published.status_code == 200, published.text

        listed = await client.get(
            f"{API}/admin/legal-documents", params={"status": "draft", "page_size": 100}
        )
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(LEGAL_DOCUMENTS, params={"status": "draft", "lang": "ru"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert exported_ids == listed_ids
        assert doc_id in exported_ids

        resp = await client.get(LEGAL_DOCUMENTS, params={"status": "draft", "lang": "uz_latn"})
        row = _row(resp.content, doc_id)
        assert row[0] == f"LBL-{marker}"  # doc_number first
        assert row[4] == "Qoralama"  # status label
        assert row[3] == "lex.uz havolasi"  # source label from source_url

        resp = await client.get(LEGAL_DOCUMENTS, params={"status": "published"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert published_id in exported_ids
        assert doc_id not in exported_ids

        with export_cap(1):  # two drafts at least, one fits
            resp = await client.get(LEGAL_DOCUMENTS, params={"status": "draft"})
            assert int(resp.headers["x-export-total"]) >= 2
            assert_export_cut(resp, cap=1)


async def test_legal_documents_export_without_the_permission_matches_the_list_status(db):
    await _same_status_without_the_permission(db, f"{API}/admin/legal-documents", LEGAL_DOCUMENTS)


# ---------------------------------------------------------------------------
# Integrations outbox — GET /admin/integrations/outbox/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_outbox_export_mirrors_the_list(db):
    _, token, csrf = await signed_in_with(db, INTEGRATIONS_VIEW)
    await db.commit()
    # The delivered message first: `deliver_one` claims the OLDEST due row
    # in this shared, persistent database, so the queue is drained until
    # OUR row has gone through (lesson: a claim-the-oldest worker takes a
    # stranger's row).
    ok_destination = f"_export_ok_{uuid.uuid4().hex[:8]}"

    async def ok_sender(_db, _payload: dict) -> None:
        return None

    senders.SENDERS[ok_destination] = ok_sender
    try:
        delivered = await integrations_service.enqueue(db, destination=ok_destination, payload={})
        assert delivered is not None
        await db.commit()
        for _ in range(50):
            await db.refresh(delivered)
            if delivered.status != "pending":
                break
            assert await integrations_service.deliver_one(db) is True
        await db.commit()
        assert delivered.status == "delivered", delivered.status
    finally:
        senders.SENDERS.pop(ok_destination, None)
    destination = f"_export_test_{uuid.uuid4().hex[:8]}"
    msg = await integrations_service.enqueue(
        db, destination=destination, payload={"secret": "should-never-appear"}
    )
    assert msg is not None
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(
            f"{API}/admin/integrations/outbox", params={"destination": destination}
        )
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(OUTBOX, params={"destination": destination, "lang": "ru"})
        assert resp.status_code == 200, resp.text
        assert _exported_ids(resp.content) == listed_ids == {str(msg.id)}

        # Labels, not codes — and never the payload.
        resp = await client.get(OUTBOX, params={"destination": destination, "lang": "uz_latn"})
        row = _row(resp.content, str(msg.id))
        assert row[0] == destination
        assert row[1] == "Navbatda"  # status label, not "pending"
        assert "should-never-appear" not in "".join(str(v) for v in row if v is not None)

        # The list's status filter: the delivered message is not pending.
        resp = await client.get(OUTBOX, params={"status": "pending", "destination": ok_destination})
        assert resp.status_code == 200, resp.text
        assert _exported_ids(resp.content) == set()
        resp = await client.get(OUTBOX, params={"status": "pending", "destination": destination})
        assert _exported_ids(resp.content) == {str(msg.id)}

        with export_cap(1):  # the pending and the delivered message at least
            resp = await client.get(OUTBOX)
            assert int(resp.headers["x-export-total"]) >= 2
            assert_export_cut(resp, cap=1)


async def test_outbox_export_without_the_permission_matches_the_list_status(db):
    await _same_status_without_the_permission(db, f"{API}/admin/integrations/outbox", OUTBOX)


# ---------------------------------------------------------------------------
# Integrations dead letters — GET /admin/integrations/dead-letters/export.xlsx
# ---------------------------------------------------------------------------


async def test_the_dead_letters_export_mirrors_the_list(db):
    manager, token, csrf = await signed_in_with(db, INTEGRATIONS_MANAGE)
    marker = uuid.uuid4().hex[:8]
    source = f"_export_label_{marker}"
    letter = await record_dead_letter(
        db, source=source, payload={"secret": "should-never-appear"}, error="bad shape"
    )
    another = await record_dead_letter(db, source=f"_new_{marker}", payload={}, error="still new")
    to_discard = await record_dead_letter(
        db, source=f"_discard_{marker}", payload={}, error="will be discarded"
    )
    await db.commit()
    await discard_dead_letter(db, to_discard.id, actor_id=manager.id, ip=None)
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        listed = await client.get(
            f"{API}/admin/integrations/dead-letters", params={"status": "new", "page_size": 100}
        )
        assert listed.status_code == 200, listed.text
        listed_ids = {row["id"] for row in listed.json()["items"]}

        resp = await client.get(DEAD_LETTERS, params={"status": "new", "lang": "ru"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert exported_ids == listed_ids
        assert {str(letter.id), str(another.id)} <= exported_ids

        # Labels, not codes — and never the payload.
        resp = await client.get(DEAD_LETTERS, params={"status": "new", "lang": "uz_latn"})
        row = _row(resp.content, str(letter.id))
        assert row[0] == source
        assert row[1] == "Yangi"  # status label, not "new"
        assert "should-never-appear" not in "".join(str(v) for v in row if v is not None)

        resp = await client.get(DEAD_LETTERS, params={"status": "discarded"})
        assert resp.status_code == 200, resp.text
        exported_ids = _exported_ids(resp.content)
        assert str(to_discard.id) in exported_ids
        assert str(letter.id) not in exported_ids

        with export_cap(1):  # three letters recorded here, one fits
            resp = await client.get(DEAD_LETTERS)
            assert int(resp.headers["x-export-total"]) >= 2
            assert_export_cut(resp, cap=1)


async def test_dead_letters_export_without_the_permission_matches_the_list_status(db):
    await _same_status_without_the_permission(
        db, f"{API}/admin/integrations/dead-letters", DEAD_LETTERS
    )
