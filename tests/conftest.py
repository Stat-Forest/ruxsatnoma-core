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
async def db(engine) -> AsyncSession:
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()
