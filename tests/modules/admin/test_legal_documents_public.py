"""`/api/v1/public/legal-documents` (`0043`) — the anonymous surface the public
site's /documents page reads. No session anywhere in this file: that is the
point of it."""

from app.main import create_app
from app.modules.admin.permissions import LEGAL_DOCUMENTS_MANAGE
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


async def _editor(db):
    _, token, csrf = await signed_in_with(db, LEGAL_DOCUMENTS_MANAGE)
    await db.commit()
    return token, csrf


async def test_the_anonymous_list_shows_published_rows_only(db):
    token, csrf = await _editor(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        draft_id = (await client.post(f"{API}/admin/legal-documents", json=BODY)).json()["id"]
        live_id = (
            await client.post(f"{API}/admin/legal-documents", json={**BODY, "doc_number": "PF-108"})
        ).json()["id"]
        await client.post(f"{API}/admin/legal-documents/{live_id}/publish")

    async with make_client(create_app(), lifespan=True) as anon:  # no session at all
        listing = await anon.get(f"{API}/public/legal-documents", params={"page_size": 100})
        live = await anon.get(f"{API}/public/legal-documents/{live_id}")
        draft = await anon.get(f"{API}/public/legal-documents/{draft_id}")

    assert listing.status_code == 200, listing.text
    listed = {item["id"] for item in listing.json()["items"]}
    assert live_id in listed
    assert draft_id not in listed
    assert live.status_code == 200
    assert live.json()["doc_number"] == "PF-108"
    assert "status" not in live.json()
    assert draft.status_code == 404
    assert draft.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_published_documents_pdf_downloads_without_a_session(db):
    """`GET /files/{id}` needs a session — without this route a public
    document's own PDF would be a link a visitor cannot open."""
    token, csrf = await _editor(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        doc_id = (
            await client.post(f"{API}/admin/legal-documents", json={**BODY, "file_id": file_id})
        ).json()["id"]
        await client.post(f"{API}/admin/legal-documents/{doc_id}/publish")

    async with make_client(create_app(), lifespan=True) as anon:
        r = await anon.get(f"{API}/public/legal-documents/{doc_id}/file")
        no_session_direct = await anon.get(f"{API}/files/{file_id}")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert "filename" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert no_session_direct.status_code == 401


async def test_a_draft_documents_file_is_a_404_not_a_403(db):
    """To the internet "not published" and "does not exist" must be one answer."""
    token, csrf = await _editor(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        doc_id = (
            await client.post(f"{API}/admin/legal-documents", json={**BODY, "file_id": file_id})
        ).json()["id"]

    async with make_client(create_app(), lifespan=True) as anon:
        r = await anon.get(f"{API}/public/legal-documents/{doc_id}/file")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_a_published_document_without_a_file_answers_404_on_the_file_route(db):
    """The lex.uz-only row: the page links out instead, and the file route has
    nothing to hand over."""
    token, csrf = await _editor(db)
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        doc_id = (await client.post(f"{API}/admin/legal-documents", json=BODY)).json()["id"]
        await client.post(f"{API}/admin/legal-documents/{doc_id}/publish")

    async with make_client(create_app(), lifespan=True) as anon:
        r = await anon.get(f"{API}/public/legal-documents/{doc_id}/file")

    assert r.status_code == 404
