import httpx
import pytest
from fastapi import APIRouter

from app.core.errors import err
from app.main import create_app


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

    app.include_router(r)
    # raise_app_exceptions=False: Starlette ре-рейзит необработанные исключения
    # после отправки 500 — иначе тест /crash увидит исключение, а не ответ
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
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
