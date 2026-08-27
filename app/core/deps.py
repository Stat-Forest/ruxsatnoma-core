"""Общие FastAPI-зависимости: сессия БД на запрос."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия БД на запрос: политика — commit-on-success.

    Сервисы могут коммитить раньше явно (например, чтобы увидеть сгенерированный
    БД id до дальнейшей обработки в том же запросе) — эта зависимость лишь
    гарантирует, что всё не закоммиченное явно будет сохранено при успешном
    выходе из хендлера и отменено при исключении.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
