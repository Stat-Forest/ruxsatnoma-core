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
        payme_mode="real",
        # Stage 5.1: a real OneID needs its credentials, a real scope and a
        # redirect URI registered in the TI (localhost is forbidden there).
        oneid_client_id="forestry_uz",
        oneid_client_secret="a-real-oneid-client-secret",
        oneid_scope="forestry_uz",
        oneid_redirect_uri="https://ruxsatnoma.example.uz/api/v1/auth/oneid/callback",
        # Stage 5.2: a real E-IMZO needs the site host its API-KEY is bound to
        # (decision #165); eimzo_base_url keeps its private-network default.
        eimzo_site_host="admin.ruxsatnoma-urmon.uz",
        eskiz_email="bot@example.uz",
        eskiz_password="a-real-eskiz-password",
        eskiz_sender="4546",
        eskiz_callback_secret="a-real-callback-secret",
        eskiz_callback_base_url="https://ruxsatnoma.example.uz",
        public_base_url="https://ruxsatnoma.example.uz",
        smtp_host="smtp.example.uz",
        smtp_from="noreply@example.uz",
        payme_merchant_id="a-real-merchant-id",
        payme_cashbox_key="a-real-cashbox-key",
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
    for name in ("ONEID_MODE", "EIMZO_MODE", "SMS_MODE", "EMAIL_MODE", "PAYME_MODE"):
        monkeypatch.setenv(name, "real")
    monkeypatch.setenv("ONEID_CLIENT_ID", "forestry_uz")
    monkeypatch.setenv("ONEID_CLIENT_SECRET", "real-oneid-secret-for-prod-guard-test")
    monkeypatch.setenv("ONEID_SCOPE", "forestry_uz")
    monkeypatch.setenv(
        "ONEID_REDIRECT_URI", "https://ruxsatnoma.example.uz/api/v1/auth/oneid/callback"
    )
    # Stage 5.2: a real E-IMZO needs the site host its API-KEY is bound to
    # (decision #165); EIMZO_BASE_URL keeps its private-network default.
    monkeypatch.setenv("EIMZO_SITE_HOST", "admin.ruxsatnoma-urmon.uz")
    monkeypatch.setenv("ESKIZ_EMAIL", "bot@example.uz")
    monkeypatch.setenv("ESKIZ_PASSWORD", "real-eskiz-password-for-prod-guard-test")
    monkeypatch.setenv("ESKIZ_SENDER", "4546")
    monkeypatch.setenv("ESKIZ_CALLBACK_SECRET", "real-callback-secret-for-prod-guard-test")
    monkeypatch.setenv("ESKIZ_CALLBACK_BASE_URL", "https://ruxsatnoma.example.uz")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ruxsatnoma.example.uz")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.uz")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.uz")
    monkeypatch.setenv("PAYME_MERCHANT_ID", "real-merchant-id-for-prod-guard-test")
    monkeypatch.setenv("PAYME_CASHBOX_KEY", "real-cashbox-key-for-prod-guard-test")
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


def test_sms_mode_real_requires_a_reachable_eskiz_callback_base_url():
    """The one Eskiz setting the guard used to miss, and the only one whose default
    (`http://localhost:8000`) is a working-looking value: a prod deploy that sets
    the four credentials and forgets the base URL still SENDS every SMS while every
    delivery report goes nowhere, leaving each notification at `sent` forever with
    no error anywhere (final whole-branch review of 3.5, finding 6; retargeted from
    `public_base_url` onto its own field by ruling #124)."""

    def _settings(**overrides: str) -> Settings:
        return Settings(
            sms_mode="real",
            eskiz_email="bot@example.uz",
            eskiz_password="a-real-eskiz-password",
            eskiz_sender="4546",
            eskiz_callback_secret="a-real-callback-secret",
            **overrides,
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )

    with pytest.raises(ValidationError, match="eskiz_callback_base_url"):
        _settings()  # the default, http://localhost:8000, looks configured but is not
    with pytest.raises(ValidationError, match="eskiz_callback_base_url"):
        _settings(eskiz_callback_base_url="http://127.0.0.1:8000")
    # public_base_url supplied too — see the next test for what happens when it
    # is NOT: ruling #124's own guard would otherwise refuse this construction
    # for an unrelated reason and this assertion would never prove its own point.
    settings = _settings(
        eskiz_callback_base_url="https://ruxsatnoma.uz", public_base_url="https://ruxsatnoma.uz"
    )
    assert settings.eskiz_callback_base_url == "https://ruxsatnoma.uz"


def test_public_base_url_must_be_reachable_once_the_eskiz_callback_is():
    """Ruling #124's own guard, same shape as `admin_base_url`'s below: the two
    fields were ONE setting (`PUBLIC_BASE_URL`) until stage 7.0, so a deploy that
    configures the callback and simply forgets its former sibling must not fall
    through to a local-looking QR default — a permit's QR is rendered once and is
    unfixable afterwards. Unconditional on `sms_mode`, deliberately: printing a
    permit needs no SMS at all."""
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(
            eskiz_callback_base_url="https://ruxsatnoma.example.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )  # the default, http://localhost:8000, looks configured but is not
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(
            eskiz_callback_base_url="https://ruxsatnoma.example.uz",
            public_base_url="http://127.0.0.1:8000",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
    assert (
        Settings(
            eskiz_callback_base_url="https://ruxsatnoma.example.uz",
            public_base_url="https://ruxsatnoma.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        ).public_base_url
        == "https://ruxsatnoma.uz"
    )


def test_admin_base_url_must_be_reachable_when_cors_names_a_deployed_origin():
    """Mirrors the sms_mode=real/public_base_url guard above: the OneID
    callback (app/modules/auth/router.py) redirects an already-authenticated
    browser (valid session cookies already set) to admin_base_url. A deploy
    that points cors_origins at the real adminka origin but forgets
    ADMIN_BASE_URL sends that browser to its own localhost — a browser
    "connection refused" with nothing in the logs to explain it (final review
    of stage 6.6, finding 2)."""
    with pytest.raises(ValidationError, match="admin_base_url"):
        Settings(
            cors_origins=["https://admin.ruxsatnoma.uz"],
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )  # the default, http://localhost:5173, looks configured but is not
    with pytest.raises(ValidationError, match="admin_base_url"):
        Settings(
            cors_origins=["https://admin.ruxsatnoma.uz"],
            admin_base_url="http://127.0.0.1:5173",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
    assert (
        Settings(
            cors_origins=["https://admin.ruxsatnoma.uz"],
            admin_base_url="https://admin.ruxsatnoma.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        ).admin_base_url
        == "https://admin.ruxsatnoma.uz"
    )


def test_admin_base_url_stays_local_for_local_multi_port_dev_cors():
    """Local dev (this repo's own .env, stage 6.0) lists several LOCALHOST
    ports in cors_origins — the adminka/landing Vite dev servers on their own
    ports talking to the API on another — and admin_base_url staying
    localhost there is correct, not the deploy-forgot-to-set-it bug the guard
    above catches. A guard keyed on plain "cors_origins is non-empty" would
    refuse this legitimate, working local configuration outright."""
    settings = Settings(
        cors_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    assert settings.admin_base_url == "http://localhost:5173"


def test_env_example_is_a_working_env_file(tmp_path, monkeypatch):
    """`cp .env.example .env` is the README's documented first step, so every entry
    in that file must be a value the app can actually start on. pydantic-settings
    does NOT ignore an empty env value: `EMAIL_MODE=` fails the Literal, `SMTP_PORT=`
    fails int, `SMTP_STARTTLS=` fails bool, and `ESKIZ_BASE_URL=` silently replaces
    a working default with "" (final whole-branch review of 3.5, finding 7)."""
    import os
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)  # the file, not this shell
    target = tmp_path / ".env"
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    assert os.environ.get("EMAIL_MODE") is None

    settings = Settings(_env_file=target)  # pyright: ignore[reportCallIssue]

    assert settings.email_mode == "mock"
    assert settings.smtp_port == 587
    assert settings.smtp_starttls is True
    assert settings.eskiz_base_url.startswith("https://")
    assert settings.public_base_url.startswith("http")
    assert settings.eskiz_callback_base_url.startswith("http")


# --- OneID go-live (stage 5.1) -------------------------------------------------
# `oneid_mode=real` is checked unconditionally, like sms_mode: a half-configured
# live provider fails at LOGIN TIME, for every citizen at once, and OneID is the
# only entry a citizen has (decision #94).

REAL_ONEID = {
    "oneid_mode": "real",
    "oneid_client_id": "forestry_uz",
    "oneid_client_secret": "a-real-client-secret",
    "oneid_scope": "forestry_uz",
    "oneid_redirect_uri": "https://dev-api.ruxsatnoma-urmon.uz/api/v1/auth/oneid/callback",
}


def test_oneid_mode_real_requires_client_credentials():
    with pytest.raises(ValidationError, match="oneid_client_id"):
        Settings(**{**REAL_ONEID, "oneid_client_id": ""}, _env_file=None)  # pyright: ignore[reportCallIssue]


def test_oneid_mode_real_requires_the_client_secret():
    with pytest.raises(ValidationError, match="oneid_client_id"):
        Settings(**{**REAL_ONEID, "oneid_client_secret": ""}, _env_file=None)  # pyright: ignore[reportCallIssue]


def test_oneid_mode_real_refuses_the_placeholder_scope():
    # At OneID the scope IS the client_id (plan 05.1 R1). The default
    # "mock-scope" would be sent to the provider verbatim and refused there.
    with pytest.raises(ValidationError, match="oneid_scope"):
        Settings(**{**REAL_ONEID, "oneid_scope": "mock-scope"}, _env_file=None)  # pyright: ignore[reportCallIssue]


def test_oneid_mode_real_refuses_a_local_redirect_uri():
    # The TI lists every allowed redirect URI and forbids localhost
    # (design/04 §1.4): a local one is refused BY THE PROVIDER, with nothing on
    # our side to explain it.
    with pytest.raises(ValidationError, match="oneid_redirect_uri"):
        Settings(
            **{
                **REAL_ONEID,
                "oneid_redirect_uri": "http://localhost:8000/api/v1/auth/oneid/callback",
            },
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )


def test_oneid_mode_real_accepts_a_complete_configuration():
    settings = Settings(**REAL_ONEID, _env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.oneid_base_url == "https://sso.egov.uz/sso/oauth/Authorization.do"
    assert settings.oneid_client_id == "forestry_uz"


def test_mock_oneid_needs_none_of_it():
    # The mock is the default in every environment that exists today, and the
    # TI forbids localhost as a redirect target — so local development can
    # never be made to satisfy the real-mode guard (design/04 §1.5).
    settings = Settings(oneid_mode="mock", _env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.oneid_scope == "mock-scope"


def test_eimzo_real_requires_base_url_and_site_host() -> None:
    with pytest.raises(ValidationError, match="eimzo_mode=real requires eimzo_base_url"):
        Settings(
            eimzo_mode="real",
            eimzo_base_url="",
            eimzo_site_host="admin.example.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
    with pytest.raises(ValidationError, match="eimzo_mode=real requires eimzo_site_host"):
        Settings(
            eimzo_mode="real",
            eimzo_base_url="http://eimzo:8080",
            eimzo_site_host="",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )


def test_eimzo_real_refuses_a_public_base_url() -> None:
    # The server must sit in the stack's private network (plan 05.2 R2). A
    # public address means somebody exposed it, and that is worth failing
    # over. "Private" is not "contains no dot": a bare Docker service name
    # (`eimzo`), `localhost` and any RFC-1918/loopback numeric address are
    # all private too, and dev/CI both run against `http://127.0.0.1:8080`.
    with pytest.raises(ValidationError, match="must not be publicly reachable"):
        Settings(
            eimzo_mode="real",
            eimzo_base_url="https://eimzo.ruxsatnoma-urmon.uz",
            eimzo_site_host="admin.ruxsatnoma-urmon.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
    for base_url in (
        "http://127.0.0.1:8080",
        "http://eimzo:8080",
        "http://10.1.2.3:8080",
    ):
        settings = Settings(
            eimzo_mode="real",
            eimzo_base_url=base_url,
            eimzo_site_host="admin.ruxsatnoma-urmon.uz",
            _env_file=None,  # pyright: ignore[reportCallIssue]
        )
        assert settings.eimzo_base_url == base_url
