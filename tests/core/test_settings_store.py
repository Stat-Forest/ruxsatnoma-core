"""Runtime policy: code defaults, DB overrides, cache, type coercion (ruling 8)."""

import pytest

from app.core import settings_store
from app.core.errors import DomainError
from app.core.models import SystemSetting


@pytest.fixture(autouse=True)
def _clear_cache():
    settings_store.invalidate()
    yield
    settings_store.invalidate()


async def test_default_when_no_row(db):
    assert await settings_store.get_int(db, "session_idle_minutes") == 300
    assert await settings_store.get_int(db, "login_max_attempts") == 5


async def test_db_row_overrides_default(db):
    db.add(SystemSetting(key="session_idle_minutes", value=45))
    await db.flush()
    assert await settings_store.get_int(db, "session_idle_minutes") == 45


async def test_value_is_cached_until_invalidated(db):
    db.add(SystemSetting(key="login_max_attempts", value=9))
    await db.flush()
    assert await settings_store.get_int(db, "login_max_attempts") == 9

    row = await db.get(SystemSetting, "login_max_attempts")
    assert row is not None
    row.value = 3
    await db.flush()
    assert await settings_store.get_int(db, "login_max_attempts") == 9  # still cached

    settings_store.invalidate("login_max_attempts")
    assert await settings_store.get_int(db, "login_max_attempts") == 3


async def test_corrupt_row_falls_back_to_default(db):
    """A hand-edited row must not take the app down."""
    db.add(SystemSetting(key="session_absolute_hours", value="twelve"))
    await db.flush()
    assert await settings_store.get_int(db, "session_absolute_hours") == 12


async def test_bool_is_not_accepted_as_int(db):
    db.add(SystemSetting(key="mfa_max_attempts", value=True))
    await db.flush()
    assert await settings_store.get_int(db, "mfa_max_attempts") == 5


async def test_unknown_key_is_a_programming_error(db):
    with pytest.raises(KeyError):
        await settings_store.get_setting(db, "no_such_setting")


def test_coerce_accepts_and_rejects():
    spec = settings_store.SETTING_SPECS["session_idle_minutes"]
    assert settings_store.coerce(spec, 45) == 45
    assert settings_store.coerce(spec, "45") == 45  # JSON bodies may carry strings
    with pytest.raises(DomainError):
        settings_store.coerce(spec, "soon")
    with pytest.raises(DomainError):
        settings_store.coerce(spec, 0)  # positive-int policy values only


def test_every_spec_has_a_description():
    assert all(spec.description for spec in settings_store.SETTING_SPECS.values())


SITE_KEYS = (
    "site_contact_phone",
    "site_contact_email",
    "site_contact_address_uz",
    "site_contact_address_ru",
    "site_contact_hours_uz",
    "site_contact_hours_ru",
    "site_social_telegram",
    "site_social_youtube",
    "site_season_windows",
    "public_permit_contour_enabled",
)


def test_site_keys_are_registered():
    missing = [key for key in SITE_KEYS if key not in settings_store.SETTING_SPECS]
    assert missing == []


async def test_contour_disclosure_is_off_until_the_agency_answers(db):
    """Ruling R2: the contour is personal geodata; the default must be OFF."""
    assert await settings_store.get_bool(db, "public_permit_contour_enabled") is False


async def test_season_windows_ship_provisional_and_shaped(db):
    windows = await settings_store.get_setting(db, "site_season_windows")
    assert set(windows) == {"grazing", "haymaking", "apiary", "recreation", "deadwood", "science"}
    assert windows["science"] == list(range(1, 13))
    assert all(1 <= month <= 12 for months in windows.values() for month in months)


def test_dict_setting_round_trips_through_coerce():
    spec = settings_store.SETTING_SPECS["site_season_windows"]
    assert settings_store.coerce(spec, {"grazing": [4, 5]}) == {"grazing": [4, 5]}
