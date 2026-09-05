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
