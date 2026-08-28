"""Cache-Control: no-store on every /api/v1/* response (finding 5, whole-branch
review): a 200 with no freshness information may be heuristically cached by a shared
cache/CDN in front of the API (RFC 9111 §4.2.2) — and /auth/me now also carries the
session's CSRF token in its body, so a cached copy would leak it to another viewer."""

from app.main import create_app
from tests.conftest import make_client


async def test_api_response_carries_no_store():
    app = create_app()

    @app.get("/api/v1/_test-cache-control-probe")
    async def _probe():
        return {"ok": True}

    async with make_client(app) as client:
        r = await client.get("/api/v1/_test-cache-control-probe")
    assert r.headers["cache-control"] == "no-store"


async def test_health_is_not_affected():
    app = create_app()
    async with make_client(app) as client:
        r = await client.get("/health")
    assert "cache-control" not in r.headers
