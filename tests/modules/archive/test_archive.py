"""`POST /archive/{object_type}/{object_id}`, `GET /archive`,
`GET /archive/{id}`, `POST /archive/{id}/verify`.

The zone tests here are the archive half of the track's one hard rule: an
actor scoped to one organization must not archive, or read the archive
register of, another organization's object."""

from app.core import storage
from app.modules.archive.permissions import ARCHIVE_MANAGE
from tests.modules.archive.conftest import _client_for, _client_with_role, make_application


async def test_archive_requires_the_permission(db, leshoz):
    application = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    await db.commit()
    async for client in _client_with_role(db, "inspector"):
        resp = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-001"


async def test_archive_an_eligible_application_moves_it_and_records_the_item(db, leshoz):
    application = await make_application(
        db, org=leshoz, status="CANCELLED", applicant_name="Тестов Тест"
    )
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        resp = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object_type"] == "application"
        assert body["object_id"] == str(application.id)
        assert body["status"] == "stored"
        assert body["organization_id"] == str(leshoz.id)
        assert len(body["content_hash"]) == 64  # sha256 hex

    # Refresh through the SAME session the fixture used to write it — a plain
    # attribute read would return the stale, already-loaded value (lesson:
    # "the db fixture session and the app's session never see each other's
    # current state").
    await db.refresh(application)
    assert application.status == "ARCHIVED"


async def test_archiving_an_ineligible_status_is_refused(db, leshoz):
    application = await make_application(db, org=leshoz, status="DRAFT", applicant_name="A A")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        resp = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ERR-ARCH-001"


async def test_archiving_twice_is_refused(db, leshoz):
    application = await make_application(db, org=leshoz, status="REJECTED", applicant_name="A A")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        first = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert first.status_code == 200
        second = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "ERR-ARCH-001"


async def test_zone_scoped_actor_cannot_archive_another_orgs_application(db, leshoz, other_leshoz):
    application = await make_application(
        db, org=other_leshoz, status="CLOSED", applicant_name="A A"
    )
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        resp = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ERR-ACL-002"


async def test_zone_scoped_actor_does_not_see_another_orgs_item_in_the_list(
    db, leshoz, other_leshoz
):
    mine = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    theirs = await make_application(db, org=other_leshoz, status="CLOSED", applicant_name="B B")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE):  # republic-wide, archives both
        m = await client.post(f"/api/v1/archive/application/{mine.id}", json={})
        t = await client.post(f"/api/v1/archive/application/{theirs.id}", json={})
        assert m.status_code == 200
        assert t.status_code == 200

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        listed = await client.get("/api/v1/archive", params={"page_size": 100})
        assert listed.status_code == 200
        ids = {row["object_id"] for row in listed.json()["items"]}
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids


async def test_verify_a_correct_snapshot_passes(db, leshoz):
    application = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        created = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        item_id = created.json()["id"]
        verified = await client.post(f"/api/v1/archive/{item_id}/verify")
        assert verified.status_code == 200
        assert verified.json()["status"] == "verified"


async def test_verify_detects_a_corrupted_snapshot(db, leshoz):
    application = await make_application(db, org=leshoz, status="CLOSED", applicant_name="A A")
    await db.commit()

    async for client in _client_for(db, ARCHIVE_MANAGE, organization_id=leshoz.id):
        created = await client.post(f"/api/v1/archive/application/{application.id}", json={})
        item = created.json()
        await storage.put_object(item["storage_ref"], b"tampered", content_type="application/json")

        verified = await client.post(f"/api/v1/archive/{item['id']}/verify")
        assert verified.status_code == 409
        assert verified.json()["error"]["code"] == "ERR-ARCH-002"
