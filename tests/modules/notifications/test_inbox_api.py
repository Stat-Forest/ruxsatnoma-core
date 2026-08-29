"""/api/v1/notifications — the user's own in-app inbox, and PUT /auth/me/language."""

from app.main import create_app
from app.modules.notifications import service
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"
EVENT = "permit.issued"


async def _signed_in(db, **overrides):
    user = await make_user(db, **overrides)
    _, token, csrf = await make_session(db, user)
    return user, token, csrf


async def test_inbox_returns_only_my_notifications(db):
    mine, token, csrf = await _signed_in(db)
    other = await make_user(db)
    await service.notify(
        db, event_code=EVENT, recipient_user_id=mine.id, params={"permit_number": "P-1"}
    )
    await service.notify(
        db, event_code=EVENT, recipient_user_id=other.id, params={"permit_number": "P-2"}
    )
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/notifications")
    assert r.status_code == 200
    texts = [item["text"] for item in r.json()["items"]]
    assert any("P-1" in t for t in texts)
    assert not any("P-2" in t for t in texts)


async def test_unread_count_then_read_then_zero(db):
    user, token, csrf = await _signed_in(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        assert (await client.get(f"{API}/notifications/unread-count")).json()["count"] == 1
        read = await client.post(f"{API}/notifications/{rows[0].id}/read")
        assert read.status_code == 200
        assert read.json()["read_at"] is not None
        assert (await client.get(f"{API}/notifications/unread-count")).json()["count"] == 0


async def test_reading_someone_elses_notification_is_a_404(db):
    _, token, csrf = await _signed_in(db)
    other = await make_user(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=other.id, params={})
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/notifications/{rows[0].id}/read")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_read_all_marks_every_unread_row(db):
    user, token, csrf = await _signed_in(db)
    for _ in range(3):
        await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/notifications/read-all")
        assert r.json()["updated"] == 3
        assert (await client.get(f"{API}/notifications/unread-count")).json()["count"] == 0


async def test_unread_filter(db):
    user, token, csrf = await _signed_in(db)
    rows = await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    await service.notify(db, event_code=EVENT, recipient_user_id=user.id, params={})
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        await client.post(f"{API}/notifications/{rows[0].id}/read")
        r = await client.get(f"{API}/notifications?unread=true")
    assert r.json()["total"] == 1


async def test_inbox_requires_authentication(db):
    async with make_client(create_app(), lifespan=True) as client:
        r = await client.get(f"{API}/notifications")
    assert r.status_code == 401


async def test_put_language_changes_rendering_language(db):
    user, token, csrf = await _signed_in(db)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(f"{API}/auth/me/language", json={"language": "ru"})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["language"] == "ru"
    await db.refresh(user)
    rows = await service.notify(
        db, event_code=EVENT, recipient_user_id=user.id, params={"permit_number": "P-9"}
    )
    assert "Разрешение P-9" in rows[0].rendered_text


async def test_put_language_rejects_an_unknown_locale(db):
    _, token, csrf = await _signed_in(db)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(f"{API}/auth/me/language", json={"language": "de"})
    assert r.status_code == 422
