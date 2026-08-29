"""Cross-origin wiring (ruling 3): CORS only when configured, Origin checked on writes."""

import pytest

from app.config import Settings, get_settings
from app.main import create_app
from tests.conftest import make_client

API = "/api/v1"
ADMIN_ORIGIN = "https://admin.ruxsatnoma.uz"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_no_cors_headers_when_not_configured():
    app = create_app()
    async with make_client(app) as client:
        r = await client.get("/health", headers={"Origin": ADMIN_ORIGIN})
    assert "access-control-allow-origin" not in r.headers


async def test_configured_origin_is_allowed(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f'["{ADMIN_ORIGIN}"]')
    get_settings.cache_clear()
    app = create_app()
    async with make_client(app) as client:
        r = await client.get("/health", headers={"Origin": ADMIN_ORIGIN})
    assert r.headers["access-control-allow-origin"] == ADMIN_ORIGIN
    assert r.headers["access-control-allow-credentials"] == "true"


async def test_unknown_origin_gets_no_allow_header(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f'["{ADMIN_ORIGIN}"]')
    get_settings.cache_clear()
    app = create_app()
    async with make_client(app) as client:
        r = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_samesite_follows_the_cross_origin_setup():
    same_origin_dev = Settings(app_env="dev", _env_file=None)  # pyright: ignore[reportCallIssue]
    assert same_origin_dev.resolve_cookie_samesite() == "lax"

    cross_origin_prod = Settings(
        app_env="prod",
        secret_key="a-real-secret-value",
        s3_secret_key="a-real-s3-secret-value",
        cors_origins=[ADMIN_ORIGIN],
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
    assert cross_origin_prod.resolve_cookie_samesite() == "none"
    assert cross_origin_prod.resolve_cookie_secure() is True

    # SameSite=None without Secure is rejected by browsers — never emit it.
    cross_origin_dev = Settings(
        app_env="dev",
        cors_origins=[ADMIN_ORIGIN],
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    assert cross_origin_dev.resolve_cookie_samesite() == "lax"
