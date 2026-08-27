"""Сборка приложения: lifespan (БД), обработчики ошибок; роутеры модулей — этап 3."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.errors import DomainError
from app.db import make_engine, make_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    yield
    await app.state.engine.dispose()


def _error_body(request: Request, code: str, message: str, details: dict | None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "correlation_id": getattr(request.state, "correlation_id", None),
        }
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Ruxsatnoma-urmon API", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # Текст исключения наружу не отдаём — только код; детали в логах
        return JSONResponse(
            status_code=500,
            content=_error_body(request, "ERR-SYS-001", "Внутренняя ошибка сервера", None),
        )

    return app
