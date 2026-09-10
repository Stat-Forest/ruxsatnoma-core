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
    "public_permit_contour_enabled",
)

# `site_contact_phone`/`site_contact_hours_uz`/`site_contact_hours_ru` ship
# with a real default and stay required; these four ship blank (no address,
# no social link yet) and must be allowed to STAY blank — an admin who fills
# one in and then clears it again must be able to (finding 4, stage 8 fix
# wave: `coerce`'s string branch used to reject `""` unconditionally, so the
# Agency could set an address but never clear it).
BLANK_ALLOWED_SITE_KEYS = (
    "site_contact_address_uz",
    "site_contact_address_ru",
    "site_social_telegram",
    "site_social_youtube",
)


def test_site_keys_are_registered():
    missing = [key for key in SITE_KEYS if key not in settings_store.SETTING_SPECS]
    assert missing == []


async def test_contour_disclosure_is_off_until_the_agency_answers(db):
    """Ruling R2: the contour is personal geodata; the default must be OFF."""
    assert await settings_store.get_bool(db, "public_permit_contour_enabled") is False


def test_blank_is_accepted_only_for_the_specs_that_allow_it():
    for key in BLANK_ALLOWED_SITE_KEYS:
        spec = settings_store.SETTING_SPECS[key]
        assert spec.allow_blank is True, key
        assert settings_store.coerce(spec, "") == ""

    phone_spec = settings_store.SETTING_SPECS["site_contact_phone"]
    assert phone_spec.allow_blank is False
    with pytest.raises(DomainError):
        settings_store.coerce(phone_spec, "")


async def test_a_blank_address_round_trips_through_get_and_set(db):
    """The real failure finding 4 named: a blank value written by the admin
    API must be readable back as the blank it is, not fall back to the
    non-blank default (or, before the fix, be refused outright by `coerce`
    at write time)."""
    key = "site_contact_address_uz"
    await settings_store.set_setting(db, key, "")
    await db.flush()
    settings_store.invalidate(key)
    assert await settings_store.get_str(db, key) == ""
