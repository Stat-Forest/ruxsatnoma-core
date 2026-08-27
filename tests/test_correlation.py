import pytest

from app.core.errors import err
from app.main import create_app
from tests.conftest import make_client


@pytest.fixture
async def client():
    app = create_app()

    @app.get("/boom")
    async def boom():
        raise err("ERR-SYS-002")

    @app.get("/crash")
    async def crash():
        raise RuntimeError("внутреннее")

    async with make_client(app) as c:
        yield c


async def test_request_id_generated(client):
    resp = await client.get("/boom")
    assert resp.headers["x-request-id"]
    assert resp.json()["error"]["correlation_id"] == resp.headers["x-request-id"]


async def test_request_id_passthrough(client):
    resp = await client.get("/boom", headers={"X-Request-Id": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"
    assert resp.json()["error"]["correlation_id"] == "abc-123"


async def test_crash_path_has_request_id(client):
    # Настоящее непойманное исключение (ERR-SYS-001) уходит в ServerErrorMiddleware
    # в обход correlation_middleware — id должен проставляться и здесь тоже.
    resp = await client.get("/crash")
    assert resp.status_code == 500
    assert resp.headers["x-request-id"]
    assert resp.json()["error"]["correlation_id"] == resp.headers["x-request-id"]

    resp = await client.get("/crash", headers={"X-Request-Id": "crash-abc-123"})
    assert resp.status_code == 500
    assert resp.headers["x-request-id"] == "crash-abc-123"
    assert resp.json()["error"]["correlation_id"] == "crash-abc-123"
