"""/api/v1/announcements (reader) + /api/v1/admin/announcements (admin CRUD):
audience-filtered visibility, permission gate, file attachment, and the files-access
grant an announcement extends to its attached media (ruling 5)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.main import create_app
from app.modules.admin.models import Announcement, Region
from app.modules.admin.permissions import ANNOUNCEMENTS_MANAGE
from app.modules.audit.models import AuditLog
from tests.conftest import make_client
from tests.core.test_files_api import PDF, PNG, registered_applicant, upload
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session

API = "/api/v1"


async def make_announcement(db, *, created_by, **overrides) -> Announcement:
    """Direct-insert factory for reader-visibility fixtures — mirrors `make_user`/
    `make_session`'s pattern (tests/modules/auth/test_sessions.py) for setup that
    doesn't need to go through the admin API under test."""
    fields = {
        "title": {"uz_cyrl": "Эълон", "uz_latn": "Eʼlon"},
        "body": {"uz_cyrl": "Матн", "uz_latn": "Matn"},
        "status": "published",
        "publish_from": datetime.now(UTC) - timedelta(minutes=1),
        "created_by": created_by,
    }
    fields.update(overrides)
    ann = Announcement(**fields)
    db.add(ann)
    await db.flush()
    return ann


# --- Admin CRUD ---------------------------------------------------------------------


async def test_create_draft_attaches_files_and_audits(db):
    actor, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Эълон", "uz_latn": "Eʼlon"},
                "body": {"uz_cyrl": "Матн", "uz_latn": "Matn"},
                "file_ids": [file_id],
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert [f["id"] for f in body["files"]] == [file_id]

    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "announcement.create",
                AuditLog.object_id == uuid.UUID(body["id"]),
            )
        )
    ).scalar_one()
    assert row.user_id == actor.id


async def test_create_rejects_unknown_file_ids(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "file_ids": [str(uuid.uuid4())],
            },
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "file_not_found"


async def test_create_dedupes_repeated_file_ids(db):
    """A repeated id in `file_ids` must not crash the replace-set insert on
    `AnnouncementFile`'s `(announcement_id, file_id)` primary key."""
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_id = (await upload(client, PDF)).json()["id"]
        r = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "file_ids": [file_id, file_id],
            },
        )
    assert r.status_code == 201, r.text
    assert [f["id"] for f in r.json()["files"]] == [file_id]


async def test_patch_replaces_file_ids(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        file_a = (await upload(client, PDF)).json()["id"]
        file_b = (await upload(client, PNG, filename="b.png", content_type="image/png")).json()[
            "id"
        ]
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "file_ids": [file_a],
            },
        )
        ann_id = created.json()["id"]
        r = await client.patch(f"{API}/admin/announcements/{ann_id}", json={"file_ids": [file_b]})
    assert r.status_code == 200, r.text
    assert [f["id"] for f in r.json()["files"]] == [file_b]


async def test_publish_sets_publish_from_and_audits(db):
    actor, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
            },
        )
        assert created.json()["publish_from"] is None
        ann_id = created.json()["id"]
        r = await client.post(f"{API}/admin/announcements/{ann_id}/publish")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["publish_from"] is not None

    row = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "announcement.publish",
                AuditLog.object_id == uuid.UUID(ann_id),
            )
        )
    ).scalar_one()
    assert row.user_id == actor.id


async def test_publish_rejects_bad_window(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    now = datetime.now(UTC)
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "publish_from": now.isoformat(),
                "publish_to": (now - timedelta(hours=1)).isoformat(),
            },
        )
        ann_id = created.json()["id"]
        r = await client.post(f"{API}/admin/announcements/{ann_id}/publish")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "bad_window"


async def test_patch_archived_is_rejected(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
            },
        )
        ann_id = created.json()["id"]
        await client.post(f"{API}/admin/announcements/{ann_id}/archive")
        r = await client.patch(
            f"{API}/admin/announcements/{ann_id}", json={"title": {"uz_cyrl": "Я", "uz_latn": "Ya"}}
        )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "archived"


async def test_archive_from_draft_and_from_published(db):
    _, token, csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        draft = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э1", "uz_latn": "E1"},
                "body": {"uz_cyrl": "М1", "uz_latn": "M1"},
            },
        )
        r_from_draft = await client.post(f"{API}/admin/announcements/{draft.json()['id']}/archive")

        published = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э2", "uz_latn": "E2"},
                "body": {"uz_cyrl": "М2", "uz_latn": "M2"},
            },
        )
        pub_id = published.json()["id"]
        await client.post(f"{API}/admin/announcements/{pub_id}/publish")
        r_from_published = await client.post(f"{API}/admin/announcements/{pub_id}/archive")

    assert r_from_draft.status_code == 200, r_from_draft.text
    assert r_from_draft.json()["status"] == "archived"
    assert r_from_published.status_code == 200, r_from_published.text
    assert r_from_published.json()["status"] == "archived"


async def test_admin_routes_require_permission(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


# --- Reader: audience-filtered, published, in-window ---------------------------------


async def test_reader_visibility_window_and_status(db):
    """Visible: published + in-window. Invisible: draft, archived, not-yet-published,
    past its window — on both the list and the detail route."""
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    _, token, csrf = await signed_in_with(db)
    now = datetime.now(UTC)
    visible = await make_announcement(
        db, created_by=admin.id, publish_from=now - timedelta(minutes=1)
    )
    draft = await make_announcement(db, created_by=admin.id, status="draft", publish_from=None)
    archived = await make_announcement(
        db, created_by=admin.id, status="archived", publish_from=now - timedelta(days=1)
    )
    future = await make_announcement(db, created_by=admin.id, publish_from=now + timedelta(days=1))
    past = await make_announcement(
        db,
        created_by=admin.id,
        publish_from=now - timedelta(days=2),
        publish_to=now - timedelta(hours=1),
    )
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        listing = await client.get(f"{API}/announcements", params={"page_size": 100})
        got_visible = await client.get(f"{API}/announcements/{visible.id}")
        got_draft = await client.get(f"{API}/announcements/{draft.id}")
        got_archived = await client.get(f"{API}/announcements/{archived.id}")
        got_future = await client.get(f"{API}/announcements/{future.id}")
        got_past = await client.get(f"{API}/announcements/{past.id}")

    assert listing.status_code == 200, listing.text
    assert str(visible.id) in {item["id"] for item in listing.json()["items"]}
    assert got_visible.status_code == 200, got_visible.text
    for r in (got_draft, got_archived, got_future, got_past):
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_reader_role_targeting(db):
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    _, staff_token, staff_csrf = await signed_in_with(db)  # executor_staff
    applicant = await registered_applicant(db)
    _, app_token, app_csrf = await make_session(db, applicant)
    now = datetime.now(UTC)
    ann = await make_announcement(
        db,
        created_by=admin.id,
        publish_from=now - timedelta(minutes=1),
        audience={"role_codes": ["executor_staff"]},
    )
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, staff_token, staff_csrf)
        r_staff = await client.get(f"{API}/announcements/{ann.id}")
    async with make_client(app, lifespan=True) as client:
        auth_client(client, app_token, app_csrf)
        r_applicant = await client.get(f"{API}/announcements/{ann.id}")

    assert r_staff.status_code == 200, r_staff.text
    assert r_applicant.status_code == 404


async def test_reader_region_targeting(db):
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    target_region_id = (
        await db.execute(select(Region.id).where(Region.code == "fergana"))
    ).scalar_one()
    other_region_id = (
        await db.execute(select(Region.id).where(Region.code == "andijan"))
    ).scalar_one()

    in_region, in_token, in_csrf = await signed_in_with(db)
    in_region.region_id = target_region_id
    out_region, out_token, out_csrf = await signed_in_with(db)
    out_region.region_id = other_region_id
    no_region, no_token, no_csrf = await signed_in_with(db)  # region_id stays None

    now = datetime.now(UTC)
    ann = await make_announcement(
        db,
        created_by=admin.id,
        publish_from=now - timedelta(minutes=1),
        audience={"region_ids": [str(target_region_id)]},
    )
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, in_token, in_csrf)
        r_in = await client.get(f"{API}/announcements/{ann.id}")
    async with make_client(app, lifespan=True) as client:
        auth_client(client, out_token, out_csrf)
        r_out = await client.get(f"{API}/announcements/{ann.id}")
    async with make_client(app, lifespan=True) as client:
        auth_client(client, no_token, no_csrf)
        r_none = await client.get(f"{API}/announcements/{ann.id}")

    assert r_in.status_code == 200, r_in.text
    assert r_out.status_code == 404
    assert r_none.status_code == 404


async def test_reader_ordering_by_publish_from_desc(db):
    admin, _, _ = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    _, token, csrf = await signed_in_with(db)
    now = datetime.now(UTC)
    older = await make_announcement(
        db, created_by=admin.id, publish_from=now - timedelta(seconds=20)
    )
    newer = await make_announcement(
        db, created_by=admin.id, publish_from=now - timedelta(seconds=10)
    )
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/announcements", params={"page_size": 100})
    assert r.status_code == 200, r.text
    ids_in_order = [
        item["id"] for item in r.json()["items"] if item["id"] in {str(newer.id), str(older.id)}
    ]
    assert ids_in_order == [str(newer.id), str(older.id)]


# --- Files access via a visible announcement (ruling 5) -------------------------------


async def test_file_access_granted_via_announcement_then_revoked_after_archive(db):
    admin, admin_token, admin_csrf = await signed_in_with(db, ANNOUNCEMENTS_MANAGE)
    stranger = await registered_applicant(db)
    _, stranger_token, stranger_csrf = await make_session(db, stranger)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, admin_token, admin_csrf)
        file_id = (await upload(client, PNG, filename="pic.png", content_type="image/png")).json()[
            "id"
        ]
        created = await client.post(
            f"{API}/admin/announcements",
            json={
                "title": {"uz_cyrl": "Э", "uz_latn": "E"},
                "body": {"uz_cyrl": "М", "uz_latn": "M"},
                "file_ids": [file_id],
            },
        )
        assert created.status_code == 201, created.text
        ann_id = created.json()["id"]
        published = await client.post(f"{API}/admin/announcements/{ann_id}/publish")
        assert published.status_code == 200, published.text

    async with make_client(app, lifespan=True) as client:
        auth_client(client, stranger_token, stranger_csrf)
        r_before = await client.get(f"{API}/files/{file_id}")

    async with make_client(app, lifespan=True) as client:
        auth_client(client, admin_token, admin_csrf)
        archived = await client.post(f"{API}/admin/announcements/{ann_id}/archive")
        assert archived.status_code == 200, archived.text

    async with make_client(app, lifespan=True) as client:
        auth_client(client, stranger_token, stranger_csrf)
        r_after = await client.get(f"{API}/files/{file_id}")

    assert r_before.status_code == 200, r_before.text
    assert r_after.status_code == 403
    assert r_after.json()["error"]["code"] == "ERR-ACL-001"
