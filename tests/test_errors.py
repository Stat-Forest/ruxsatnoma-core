import pytest
from fastapi import APIRouter

from app.core.errors import err
from app.main import create_app
from tests.conftest import make_client


@pytest.fixture
async def client():
    app = create_app()
    r = APIRouter()

    @r.get("/boom")
    async def boom():
        raise err("ERR-APP-002", details={"existing_application": "RX-2026-000001"})

    @r.get("/crash")
    async def crash():
        raise RuntimeError("внутреннее")

    @r.get("/typed")
    async def typed(n: int):
        return {"n": n}

    app.include_router(r)
    async with make_client(app) as c:
        yield c


async def test_domain_error_format(client):
    resp = await client.get("/boom")
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "ERR-APP-002"
    assert body["details"]["existing_application"] == "RX-2026-000001"
    assert body["message"]  # человеческий текст
    assert "correlation_id" in body


async def test_unhandled_becomes_err_sys_001(client):
    resp = await client.get("/crash")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "ERR-SYS-001"
    assert "внутреннее" not in resp.text  # текст исключения наружу не течёт


async def test_not_found_returns_err_sys_003(client):
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()["error"]
    assert body["code"] == "ERR-SYS-003"
    assert body["correlation_id"]


async def test_method_not_allowed_returns_err_sys_004(client):
    resp = await client.post("/boom")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "ERR-SYS-004"


async def test_validation_error_returns_err_val_001(client):
    resp = await client.get("/typed", params={"n": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "ERR-VAL-001"
    assert body["details"]["errors"]
