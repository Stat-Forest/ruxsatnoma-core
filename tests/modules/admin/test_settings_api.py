"""GET/PUT /api/v1/admin/settings: code defaults, DB overrides, audit, cache reset."""

import pytest
from sqlalchemy import delete, select

from app.core import settings_store
from app.core.models import SystemSetting
from app.main import create_app
from app.modules.admin.permissions import SETTINGS_MANAGE
from app.modules.audit.models import AuditLog
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

API = "/api/v1"

# Unlike classifier/organization admin tests, a setting key cannot be given a
# per-run uuid suffix — SETTING_SPECS is a fixed, shared vocabulary. The write
# endpoint commits through the app's own db session (get_db commits on success),
# so an override made here survives on the persistent test database beyond this
# test. Reset both before (defensive, in case an earlier run was interrupted) and
# after every test in this file so the suite stays green across repeated runs and
# does not leak a stale override into tests/core/test_settings_store.py, which
# asserts the code defaults.
_TOUCHED_KEYS = ("session_idle_minutes", "login_max_attempts")


@pytest.fixture(autouse=True)
async def _pristine_settings(db):
    async def _reset() -> None:
        await db.execute(delete(SystemSetting).where(SystemSetting.key.in_(_TOUCHED_KEYS)))
        await db.commit()
        settings_store.invalidate()

    await _reset()
    yield
    await _reset()


async def test_settings_require_permission(db):
    _, token, csrf = await signed_in_with(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/settings")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_list_shows_defaults(db):
    settings_store.invalidate()
    _, token, csrf = await signed_in_with(db, SETTINGS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/settings")
    assert r.status_code == 200
    rows = {row["key"]: row for row in r.json()}
    assert set(rows) == set(settings_store.SETTING_SPECS)
    assert rows["session_idle_minutes"]["value"] == 300
    assert rows["session_idle_minutes"]["default"] == 300
    assert rows["session_idle_minutes"]["overridden"] is False
    assert rows["session_idle_minutes"]["description"]


async def test_update_setting_persists_and_invalidates_cache(db):
    settings_store.invalidate()
    user, token, csrf = await signed_in_with(db, SETTINGS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        updated = await client.put(f"{API}/admin/settings/session_idle_minutes", json={"value": 45})
        listed = await client.get(f"{API}/admin/settings")
    assert updated.status_code == 200
    assert updated.json()["value"] == 45
    assert updated.json()["overridden"] is True
    assert {row["key"]: row["value"] for row in listed.json()}["session_idle_minutes"] == 45

    row = await db.get(SystemSetting, "session_idle_minutes")
    assert row is not None and row.value == 45 and row.updated_by == user.id
    assert await settings_store.get_int(db, "session_idle_minutes") == 45

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "setting.update")
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value == {"value": 300} and entry.new_value == {"value": 45}


async def test_update_rejects_bad_value(db):
    _, token, csrf = await signed_in_with(db, SETTINGS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        text_value = await client.put(
            f"{API}/admin/settings/login_max_attempts", json={"value": "soon"}
        )
        zero = await client.put(f"{API}/admin/settings/login_max_attempts", json={"value": 0})
    assert text_value.status_code == 422
    assert text_value.json()["error"]["code"] == "ERR-VAL-001"
    assert zero.status_code == 422
    assert zero.json()["error"]["details"]["reason"] == "must be positive"


async def test_unknown_setting_is_404(db):
    _, token, csrf = await signed_in_with(db, SETTINGS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(f"{API}/admin/settings/no_such_key", json={"value": 1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
