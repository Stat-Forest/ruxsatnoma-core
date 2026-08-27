from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import make_engine, make_session_factory


@pytest.fixture(scope="session")
async def engine():
    eng = make_engine(get_settings().database_url_test)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()


@asynccontextmanager
async def make_client(
    app, *, lifespan: bool = False, raise_app_exceptions: bool = False
) -> AsyncIterator[httpx.AsyncClient]:
    """Общий тестовый HTTP-клиент поверх ASGI-приложения.

    lifespan=True — поднимает app.router.lifespan_context (нужно роутам с БД,
    т.к. ASGITransport сам lifespan не запускает).
    raise_app_exceptions=False (по умолчанию) — ASGITransport не ре-рейзит
    необработанные исключения хендлеров наружу, а отдаёт итоговый HTTP-ответ
    (нужно, чтобы проверять реальный 500-ответ, а не traceback в тесте).
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        if lifespan:
            async with app.router.lifespan_context(app):
                yield client
        else:
            yield client
