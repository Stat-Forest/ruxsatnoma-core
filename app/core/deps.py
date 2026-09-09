"""Общие FastAPI-зависимости: сессия БД на запрос.

Политика транзакции распределена между двумя половинами этого файла:
`get_db` открывает сессию и откатывает её при исключении, а
`CommitBeforeResponseMiddleware` фиксирует её на успешном пути — ДО того, как
ответ уходит клиенту. Держать обе половины здесь намеренно: разнесённые по
разным модулям, они расходятся.
"""

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_TRANSACTION_KEY = "db_transaction"


class RequestTransaction:
    """Ссылка на сессию запроса, через которую middleware её коммитит.

    `settled` означает «эта сессия уже закрыта тем, кто её вёл»: middleware
    закоммитил или `get_db` откатил. Второй раз никто её не трогает.
    """

    def __init__(self) -> None:
        self.session: AsyncSession | None = None
        self.settled = False

    async def commit(self) -> None:
        if self.session is None or self.settled:
            return
        self.settled = True
        await self.session.commit()


class CommitBeforeResponseMiddleware:
    """Коммитит транзакцию запроса ПЕРЕД отправкой ответа.

    FastAPI (>= 0.106) завершает зависимости с `yield` уже ПОСЛЕ того, как ответ
    передан серверу, поэтому `commit()` в `get_db` фиксировал запись позже, чем
    клиент получал ответ. На dev это стоило рабочего входа в систему: `POST
    /auth/login` отдавал 200 с cookie, а запрос через ~6 мс получал
    `ERR-AUTH-002` — сессии в БД ещё не было. Админка читает этот код как
    «сессия кончилась» и выбрасывает на /login.

    Коммит происходит на `http.response.start`, до передачи сообщения дальше:
    ответ физически не может уйти раньше записи. Статус >= 400 не коммитим —
    это либо путь исключения (там `get_db` уже откатил), либо ответ, который
    завершит сам `get_db` ровно так, как делал раньше.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tx = RequestTransaction()
        state: dict[str, Any] = scope.setdefault("state", {})
        state[REQUEST_TRANSACTION_KEY] = tx

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                await tx.commit()
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия БД на запрос: политика — commit-on-success.

    Сервисы могут коммитить раньше явно (например, чтобы увидеть сгенерированный
    БД id до дальнейшей обработки в том же запросе) — эта зависимость лишь
    гарантирует, что всё не закоммиченное явно будет сохранено при успешном
    выходе из хендлера и отменено при исключении.

    Сам коммит на успешном пути делает `CommitBeforeResponseMiddleware` — здесь
    остаётся запасной вариант для запроса, до которого middleware не дошёл
    (не-HTTP scope, подменённая зависимость в тесте).
    """
    factory = request.app.state.session_factory
    tx: RequestTransaction | None = getattr(request.state, REQUEST_TRANSACTION_KEY, None)
    async with factory() as session:
        if tx is not None:
            tx.session = session
        try:
            yield session
        except BaseException:
            if tx is not None:
                tx.settled = True  # никто больше не должен коммитить эту сессию
            await session.rollback()
            raise
        else:
            if tx is None or not tx.settled:
                await session.commit()
