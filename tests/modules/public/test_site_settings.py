"""`GET /public/site-settings` — feeds the landing footer and its season
calendar in one anonymous request (ruling R3). The whitelist is the point:
`system_settings` also holds operational parameters (`login_max_attempts`,
`mfa_enabled`, `session_absolute_hours`, ...) that must never appear here."""

from sqlalchemy import delete

from app.core import settings_store
from app.core.models import SystemSetting
from app.core.settings_store import set_setting
from app.main import create_app
from tests.conftest import make_client

API = "/api/v1"


async def test_contacts_are_public_and_shaped(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        response = await client.get(f"{API}/public/site-settings")
    assert response.status_code == 200
    body = response.json()
    assert body["contacts"]["phone"] == "+998 71 207 88 77"
    assert body["contacts"]["address"] == {"uz_latn": "", "ru": ""}
    assert body["contacts"]["hours"]["uz_latn"].startswith("Dushanba")


async def test_operational_settings_never_leak(db) -> None:
    """The store also holds auth parameters; this route serves a whitelist."""
    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/site-settings")).json()
    flat = str(body)
    for secret in ("login_max_attempts", "mfa_enabled", "session_absolute_hours"):
        assert secret not in flat


async def test_season_windows_travel_with_the_contacts(db) -> None:
    async with make_client(create_app(), lifespan=True) as client:
        body = (await client.get(f"{API}/public/site-settings")).json()
    assert set(body["season_windows"]) == {
        "grazing",
        "haymaking",
        "apiary",
        "recreation",
        "deadwood",
        "science",
    }
    assert body["season_windows"]["science"] == list(range(1, 13))


async def test_an_edited_contact_is_served(db) -> None:
    """`set_setting` takes no `updated_by` (that lives on the row, written
    directly) and does not invalidate the process cache itself — the caller's
    job, same as every other direct writer in this suite
    (`tests/core/test_ratelimit.py`'s `_low_login_limit` fixture)."""
    key = "site_contact_phone"
    try:
        await set_setting(db, key, "+998 71 000 00 00")
        await db.commit()
        settings_store.invalidate(key)

        async with make_client(create_app(), lifespan=True) as client:
            body = (await client.get(f"{API}/public/site-settings")).json()
        assert body["contacts"]["phone"] == "+998 71 000 00 00"
    finally:
        await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
        await db.commit()
        settings_store.invalidate(key)
