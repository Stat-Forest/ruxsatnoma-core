"""`/api/v1/admin/legal-documents` — the editor's six routes and the door they
sit behind. The permission is `admin.legal_documents.manage`, its own rather
than a reuse of the announcements one (plan 07.8 R6)."""

from app.main import create_app
from app.modules.admin.permissions import ANNOUNCEMENTS_MANAGE, LEGAL_DOCUMENTS_MANAGE
from tests.conftest import make_client
from tests.core.test_files_api import PDF, upload
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1"
BODY = {
    "title": {"uz_latn": "O'rmon kodeksi", "ru": "Лесной кодекс"},
    "doc_number": "ZRU-475",
    "adopted_on": "2018-04-16",
    "source_url": "https://lex.uz/docs/3799819",
}


async def test_create_publish_and_archive_through_the_api(db):
    actor, token, csrf = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(f"{API}/admin/legal-documents", json=BODY)
        assert created.status_code == 201, created.text
        doc_id = created.json()["id"]
        assert created.json()["status"] == "draft"

        patched = await client.patch(
            f"{API}/admin/legal-documents/{doc_id}", json={"sort_order": 5}
        )
        published = await client.post(f"{API}/admin/legal-documents/{doc_id}/publish")
        listed = await client.get(f"{API}/admin/legal-documents", params={"status": "published"})
        archived = await client.post(f"{API}/admin/legal-documents/{doc_id}/archive")

    assert patched.status_code == 200, patched.text
    assert patched.json()["sort_order"] == 5
    assert published.json()["status"] == "published"
    assert doc_id in {item["id"] for item in listed.json()["items"]}
    assert archived.json()["status"] == "archived"


async def test_publishing_a_document_with_nothing_to_open_is_refused(db):
    actor, token, csrf = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        bare = {k: v for k, v in BODY.items() if k != "source_url"}
        doc_id = (await client.post(f"{API}/admin/legal-documents", json=bare)).json()["id"]
        r = await client.post(f"{API}/admin/legal-documents/{doc_id}/publish")

    assert r.status_code == 422, r.text  # ERR-VAL-001's own status
    assert r.json()["error"]["code"] == "ERR-VAL-001"
    assert r.json()["error"]["details"] == {"reason": "nothing_to_open"}


async def test_a_pdf_can_be_attached_and_comes_back_on_the_row(db):
    actor, token, csrf = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r = await client.post(f"{API}/admin/legal-documents", json={**BODY, "file_id": file_id})

    assert r.status_code == 201, r.text
    assert r.json()["file"]["id"] == file_id
    assert r.json()["file"]["content_type"] == "application/pdf"


async def test_every_route_is_refused_without_the_permission(db):
    """The announcements grant is deliberately not enough: same editor in
    practice, different door (R6)."""
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        fake = "00000000-0000-0000-0000-000000000000"
        answers = [
            await client.get(f"{API}/admin/legal-documents"),
            await client.get(f"{API}/admin/legal-documents/{fake}"),
            await client.post(f"{API}/admin/legal-documents", json=BODY),
            await client.patch(f"{API}/admin/legal-documents/{fake}", json={"sort_order": 1}),
            await client.post(f"{API}/admin/legal-documents/{fake}/publish"),
            await client.post(f"{API}/admin/legal-documents/{fake}/archive"),
        ]

    assert [r.status_code for r in answers] == [403] * 6


async def test_the_admin_routes_need_a_session_at_all(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(f"{API}/admin/legal-documents")
    assert r.status_code == 401
