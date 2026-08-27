import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # Изоляция кеша get_settings() между тестами (и от других тестовых модулей,
    # которые могли успеть его прогреть через create_app()).
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("APP_ENV", "test")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]  # env only, no .env file
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert s.app_env == "test"


def test_get_settings_cached():
    assert get_settings() is get_settings()


def test_prod_rejects_default_secret_key():
    with pytest.raises(ValidationError):
        Settings(app_env="prod", secret_key="change-me", _env_file=None)  # pyright: ignore[reportCallIssue]


def test_prod_accepts_custom_secret_key():
    s = Settings(app_env="prod", secret_key="a-real-secret-value", _env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.secret_key == "a-real-secret-value"


def test_policy_settings_are_not_deployment_config():
    """session/lockout policy lives in system_settings, not in env (stage 3.3a ruling 9)."""
    for field in (
        "session_absolute_hours",
        "session_idle_minutes",
        "login_max_attempts",
        "login_lockout_minutes",
        "mfa_token_ttl_minutes",
        "mfa_max_attempts",
    ):
        assert field not in Settings.model_fields
