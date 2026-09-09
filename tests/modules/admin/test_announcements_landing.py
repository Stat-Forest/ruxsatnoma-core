"""`/api/v1/public/announcements` (`0037`) — the anonymous surface the public
`landing` site reads: the `public_on_landing` flag, its refusal to coexist with
an audience, and the attachment download that exists because `GET /files/{id}`
needs a session."""

import uuid
from datetime import UTC, datetime, timedelta

from app.main import create_app
from app.modules.admin import announcements_service as service
from app.modules.admin.permissions import ANNOUNCEMENTS_MANAGE
from tests.conftest import make_client
from tests.core.test_files_api import PDF, upload
from tests.modules.admin.test_announcements import make_announcement
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1"


async def test_only_flagged_published_in_window_rows_reach_the_public_site(db):
    """The four ways a row stays off the public site: no flag, still a draft, not
    yet in its window, past it. The flagless one is the point of the whole
    feature — every announcement written before `0037` is one of those."""
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    now = datetime.now(UTC)
    public = await make_announcement(
        db, created_by=admin.id, public_on_landing=True, publish_from=now - timedelta(minutes=1)
    )
    internal = await make_announcement(
        db, created_by=admin.id, publish_from=now - timedelta(minutes=1)
    )
    draft = await make_announcement(
        db, created_by=admin.id, public_on_landing=True, status="draft", publish_from=None
    )
    future = await make_announcement(
        db, created_by=admin.id, public_on_landing=True, publish_from=now + timedelta(days=1)
    )
    expired = await make_announcement(
        db,
        created_by=admin.id,
        public_on_landing=True,
        publish_from=now - timedelta(days=2),
        publish_to=now - timedelta(hours=1),
    )
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:  # no session at all
        listing = await client.get(f"{API}/public/announcements", params={"page_size": 100})
        got_public = await client.get(f"{API}/public/announcements/{public.id}")
        misses = [
            await client.get(f"{API}/public/announcements/{row.id}")
            for row in (internal, draft, future, expired)
        ]

    assert listing.status_code == 200, listing.text
    listed = {item["id"] for item in listing.json()["items"]}
    assert str(public.id) in listed
    for row in (internal, draft, future, expired):
        assert str(row.id) not in listed
    assert got_public.status_code == 200, got_public.text
    for r in misses:
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_the_public_shape_hides_publish_to_and_the_admin_internals(db):
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    ann = await make_announcement(
        db,
        created_by=admin.id,
        public_on_landing=True,
        publish_from=datetime.now(UTC) - timedelta(minutes=1),
        publish_to=datetime.now(UTC) + timedelta(days=30),
    )
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(f"{API}/public/announcements/{ann.id}")

    assert r.status_code == 200, r.text
    assert set(r.json()) == {"id", "title", "body", "publish_from", "files"}


async def test_the_list_is_newest_first(db):
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    now = datetime.now(UTC)
    older = await make_announcement(
        db, created_by=admin.id, public_on_landing=True, publish_from=now - timedelta(days=3)
    )
    newer = await make_announcement(
        db, created_by=admin.id, public_on_landing=True, publish_from=now - timedelta(hours=1)
    )
    await db.commit()

    async with make_client(create_app(), lifespan=True) as client:
        listing = await client.get(f"{API}/public/announcements", params={"page_size": 100})

    ids = [item["id"] for item in listing.json()["items"]]
    assert ids.index(str(newer.id)) < ids.index(str(older.id))


# --- The flag cannot be combined with an audience ------------------------------------


async def test_create_refuses_a_targeted_public_announcement(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "audience": {"role_codes": ["executor_staff"]},
                "public_on_landing": True,
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "targeted_cannot_be_public"


async def test_patch_refuses_to_target_an_already_public_announcement(db):
    """The half-and-half case: the flag is stored, the audience arrives alone.
    Validating only what the request carries would let this through."""
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "public_on_landing": True,
            },
        )
        assert created.json()["public_on_landing"] is True
        ann_id = created.json()["id"]
        r = await client.patch(
            f"{API}/admin/announcements/{ann_id}",
            json={"audience": {"region_ids": [str(uuid.uuid4())]}},
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "targeted_cannot_be_public"


async def test_patch_refuses_to_publish_a_targeted_announcement_to_the_site(db):
    """The mirror case: the audience is stored, the flag arrives alone."""
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "audience": {"role_codes": ["executor_staff"]},
            },
        )
        ann_id = created.json()["id"]
        r = await client.patch(
            f"{API}/admin/announcements/{ann_id}", json={"public_on_landing": True}
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "targeted_cannot_be_public"


async def test_patch_can_drop_the_audience_and_go_public_in_one_request(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "audience": {"role_codes": ["executor_staff"]},
            },
        )
        ann_id = created.json()["id"]
        r = await client.patch(
            f"{API}/admin/announcements/{ann_id}",
            json={"audience": None, "public_on_landing": True},
        )
    assert r.status_code == 200, r.text
    assert r.json()["audience"] is None
    assert r.json()["public_on_landing"] is True


# --- Attachments ----------------------------------------------------------------------


async def _public_announcement_with_a_file(db, client) -> tuple[str, str]:
    file_id = (await upload(client, PDF, filename="qaror.pdf")).json()["id"]
    created = await client.post(
        f"{API}/admin/announcements",
        json={
            "title": {"uz_cyrl": "Э", "uz_latn": "E"},
            "body": {"uz_cyrl": "М", "uz_latn": "M"},
            "public_on_landing": True,
            "file_ids": [file_id],
        },
    )
    ann_id = created.json()["id"]
    await client.post(f"{API}/admin/announcements/{ann_id}/publish")
    return ann_id, file_id


async def test_a_visitor_with_no_session_can_download_an_attachment(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        ann_id, file_id = await _public_announcement_with_a_file(db, client)

    async with make_client(app, lifespan=True) as anon:
        listed = await anon.get(f"{API}/public/announcements/{ann_id}")
        r = await anon.get(f"{API}/public/announcements/{ann_id}/files/{file_id}")
        # The same file through the authenticated door stays shut without a session.
        direct = await anon.get(f"{API}/files/{file_id}")

    assert [f["id"] for f in listed.json()["files"]] == [file_id]
    assert r.status_code == 200, r.text
    assert r.content == PDF
    assert "qaror.pdf" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert direct.status_code == 401


async def test_an_attachment_of_a_non_public_announcement_is_404(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "file_ids": [file_id],
            },
        )
        ann_id = created.json()["id"]
        await client.post(f"{API}/admin/announcements/{ann_id}/publish")

    async with make_client(app, lifespan=True) as anon:
        r = await anon.get(f"{API}/public/announcements/{ann_id}/files/{file_id}")
    assert r.status_code == 404


async def test_an_attachment_is_addressed_through_its_own_announcement(db):
    """A file id alone must not open a door: the same file under a DIFFERENT
    public announcement's path is a miss, not a hit."""
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        _, file_id = await _public_announcement_with_a_file(db, client)
        other_id, _ = await _public_announcement_with_a_file(db, client)

    async with make_client(app, lifespan=True) as anon:
        r = await anon.get(f"{API}/public/announcements/{other_id}/files/{file_id}")
    assert r.status_code == 404


async def test_the_service_reader_agrees_with_the_route(db):
    """The in-process half of the surface: `landing`'s pages go through HTTP, but
    `list_landing`/`get_landing` are module functions too, and a route test proves
    only the route (lessons.md: "a green test proves nothing until you have seen
    it go red" — a public surface's own end-to-end test can ship the surface
    untested)."""
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    ann = await make_announcement(
        db,
        created_by=admin.id,
        public_on_landing=True,
        publish_from=datetime.now(UTC) - timedelta(minutes=1),
    )
    hidden = await make_announcement(db, created_by=admin.id, publish_from=datetime.now(UTC))
    await db.commit()

    from app.core.schemas import PageParams

    page = await service.list_landing(db, params=PageParams(page=1, page_size=100))
    one = await service.get_landing(db, announcement_id=ann.id)

    assert ann.id in {item.id for item in page.items}
    assert hidden.id not in {item.id for item in page.items}
    assert one.title == ann.title
