import httpx
import pytest

from app.core.errors import err
from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise err("ERR-SYS-002")

    # raise_app_exceptions=False: как в tests/test_errors.py — иначе исключения
    # из обработчиков могут прорваться наружу вместо HTTP-ответа
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_request_id_generated(client):
    resp = await client.get("/boom")
    assert resp.headers["x-request-id"]
    assert resp.json()["error"]["correlation_id"] == resp.headers["x-request-id"]


async def test_request_id_passthrough(client):
    resp = await client.get("/boom", headers={"X-Request-Id": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"
    assert resp.json()["error"]["correlation_id"] == "abc-123"
