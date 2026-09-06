"""/api/v1/admin/notification-templates: permission gate, versioning by supersede,
and the moderation warning SMS writes carry back (ruling 8)."""

import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.notifications.models import NotificationTemplate
from app.modules.notifications.permissions import TEMPLATES_MANAGE
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1/admin/notification-templates"


def _payload(event_code: str, channel: str = "inapp") -> dict:
    return {
        "event_code": event_code,
        "channel": channel,
        "body": {"uz_cyrl": "Матн {x}", "uz_latn": "Matn {x}", "ru": "Текст {x}"},
    }


async def test_create_requires_the_permission(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(API, json=_payload("test.perm"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_then_supersede_bumps_the_version_and_archives_the_old_row(db):
    actor, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        assert created.status_code == 201, created.text
        assert created.json()["version"] == 1
        first_id = created.json()["id"]

        again = await client.post(API, json=_payload(code))
        assert again.status_code == 422
        assert again.json()["error"]["details"]["reason"] == "active_version_exists"

        body = _payload(code)
        body["body"]["ru"] = "Новый текст {x}"
        superseded = await client.post(f"{API}/{first_id}", json=body)
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["version"] == 2
    old = await db.get(NotificationTemplate, uuid.UUID(first_id))
    assert old is not None
    await db.refresh(old)
    assert old.status == "archived"
    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "notification_template.supersede",
                AuditLog.object_id == uuid.UUID(superseded.json()["id"]),
            )
        )
    ).scalar_one()
    assert entry.user_id == actor.id


async def test_supersede_unknown_template_is_404(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/{uuid.uuid4()}", json=_payload(f"test.t{uuid.uuid4().hex[:8]}")
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_supersede_an_already_archived_template_is_rejected(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        template_id = created.json()["id"]
        archived = await client.post(f"{API}/{template_id}/archive")
        assert archived.status_code == 200, archived.text
        r = await client.post(f"{API}/{template_id}", json=_payload(code))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "already archived"


async def test_supersede_rejects_a_changed_event_code(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        other_code = f"test.t{uuid.uuid4().hex[:8]}"
        r = await client.post(f"{API}/{created.json()['id']}", json=_payload(other_code))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "event_code and channel must match"


async def test_sms_writes_carry_the_moderation_warning(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(API, json=_payload(f"test.t{uuid.uuid4().hex[:8]}", channel="sms"))
    assert r.status_code == 201
    assert "approved by the provider" in r.json()["warning"]


async def test_inapp_writes_carry_no_warning(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(API, json=_payload(f"test.t{uuid.uuid4().hex[:8]}"))
    assert r.json()["warning"] is None


async def test_archive_then_a_fresh_create_is_allowed(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        archived = await client.post(f"{API}/{created.json()['id']}/archive")
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        again = await client.post(API, json=_payload(code))
    assert again.status_code == 201
    assert again.json()["version"] == 2


async def test_archive_unknown_template_is_404(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/{uuid.uuid4()}/archive")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_archive_an_already_archived_template_is_rejected(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        template_id = created.json()["id"]
        first = await client.post(f"{API}/{template_id}/archive")
        assert first.status_code == 200, first.text
        r = await client.post(f"{API}/{template_id}/archive")
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "already archived"


async def test_get_template_returns_it_and_404s_for_an_unknown_id(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    code = f"test.t{uuid.uuid4().hex[:8]}"
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(API, json=_payload(code))
        found = await client.get(f"{API}/{created.json()['id']}")
        missing = await client.get(f"{API}/{uuid.uuid4()}")
    assert found.status_code == 200, found.text
    assert found.json()["id"] == created.json()["id"]
    assert found.json()["event_code"] == code
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ERR-SYS-003"


async def test_seeded_templates_are_listed(db):
    _, token, csrf = await signed_in_with(db, TEMPLATES_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}?event_code=permit.issued")
    assert r.status_code == 200
    assert {item["channel"] for item in r.json()["items"]} == {"inapp", "sms"}
