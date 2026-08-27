import pytest

from app.core.deps import get_db
from app.main import create_app
from tests.conftest import make_client


@pytest.fixture
async def client():
    app = create_app()
    async with make_client(app, lifespan=True) as c:
        yield c


async def test_liveness(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_with_db(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["postgis"].startswith("3.")


async def test_readiness_db_down_returns_503():
    # БД-заглушка, у которой execute() всегда падает — имитирует недоступную БД
    # без реального обрыва соединения.
    class _BoomingSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("БД недоступна (тест)")

    async def _failing_db():
        yield _BoomingSession()

    app = create_app()
    app.dependency_overrides[get_db] = _failing_db
    async with make_client(app, lifespan=True) as client:
        resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "ERR-SYS-002"
