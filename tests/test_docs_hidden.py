"""GET /docs, /redoc, /openapi.json — enabled only in app_env=dev (never test/prod)."""

import pytest

from app.config import get_settings
from app.main import create_app
from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_docs_enabled_in_dev():
    app = create_app()
    async with make_client(app) as client:
        r = await client.get("/docs")
    assert r.status_code == 200


async def test_docs_hidden_outside_dev(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    app = create_app()
    async with make_client(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await client.get(path)
            assert r.status_code == 404, path
            assert r.json()["error"]["code"] == "ERR-SYS-003"
