import httpx
import pytest

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    # ASGITransport не запускает lifespan — стартуем вручную
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        async with app.router.lifespan_context(app):
            yield c


async def test_liveness(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_with_db(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["postgis"].startswith("3.")
