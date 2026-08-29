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
    s = Settings(
        app_env="prod",
        secret_key="a-real-secret-value",
        s3_secret_key="a-real-s3-secret-value",
        oneid_mode="real",
        eimzo_mode="real",
        sms_mode="real",
        email_mode="real",
        eskiz_email="bot@example.uz",
        eskiz_password="a-real-eskiz-password",
        eskiz_sender="4546",
        eskiz_callback_secret="a-real-callback-secret",
        smtp_host="smtp.example.uz",
        smtp_from="noreply@example.uz",
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    assert s.secret_key == "a-real-secret-value"


def test_prod_rejects_default_s3_secret_key():
    with pytest.raises(ValidationError, match="s3_secret_key"):
        Settings(
            app_env="prod",
            secret_key="a-real-secret-value",
            oneid_mode="real",
            eimzo_mode="real",
            sms_mode="real",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )


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


def test_prod_rejects_mock_adapters(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("SECRET_KEY", "real-secret-for-prod-guard-test")
    monkeypatch.setenv("S3_SECRET_KEY", "real-s3-secret-for-prod-guard-test")
    with pytest.raises(ValidationError, match="mock adapters"):
        Settings()


def test_prod_accepts_real_adapters(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("SECRET_KEY", "real-secret-for-prod-guard-test")
    monkeypatch.setenv("S3_SECRET_KEY", "real-s3-secret-for-prod-guard-test")
    for name in ("ONEID_MODE", "EIMZO_MODE", "SMS_MODE", "EMAIL_MODE"):
        monkeypatch.setenv(name, "real")
    monkeypatch.setenv("ESKIZ_EMAIL", "bot@example.uz")
    monkeypatch.setenv("ESKIZ_PASSWORD", "real-eskiz-password-for-prod-guard-test")
    monkeypatch.setenv("ESKIZ_SENDER", "4546")
    monkeypatch.setenv("ESKIZ_CALLBACK_SECRET", "real-callback-secret-for-prod-guard-test")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.uz")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.uz")
    settings = Settings()
    assert settings.oneid_mode == "real"


def test_sms_mode_real_requires_eskiz_credentials():
    # sms_mode=real is checked unconditionally (not just under app_env=prod), and
    # ALL four Eskiz fields are required — leave exactly one empty to prove the
    # `all(...)` guard, not just an "all missing" case.
    with pytest.raises(ValidationError, match="sms_mode=real requires"):
        Settings(
            sms_mode="real",
            eskiz_email="bot@example.uz",
            eskiz_password="a-real-eskiz-password",
            eskiz_sender="4546",
            eskiz_callback_secret="",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )


def test_email_mode_real_requires_smtp_host_and_from():
    with pytest.raises(ValidationError, match="email_mode=real requires"):
        Settings(
            email_mode="real",
            smtp_host="",
            smtp_from="",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
