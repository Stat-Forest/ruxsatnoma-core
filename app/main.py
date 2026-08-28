"""Сборка приложения: lifespan (БД), обработчики ошибок; роутеры модулей — этап 3."""

import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.core import storage
from app.core.errors import ERRORS, DomainError
from app.core.health import router as health_router
from app.core.logging import CORRELATION_ID_KEY, configure_logging
from app.db import make_engine, make_session_factory
from app.files_router import router as files_router
from app.modules.admin.refs_router import router as refs_router
from app.modules.admin.router import router as admin_router
from app.modules.admin.users_router import router as users_router
from app.modules.auth.router import router as auth_router

# HTTPException с этими статусами — по коду из каталога ERR-*; остальные статусы
# (используются редко: собственный HTTPException модуля вне err()) — код ERR-SYS-001,
# статус исходного исключения сохраняется как есть.
_HTTP_STATUS_TO_CODE: dict[int, str] = {
    401: "ERR-AUTH-001",
    403: "ERR-ACL-001",
    404: "ERR-SYS-003",
    405: "ERR-SYS-004",
    429: "ERR-AUTH-003",
    503: "ERR-SYS-002",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    # Fresh dev/test MinIO volumes have no bucket yet; in prod this is an idempotent HEAD.
    await storage.ensure_bucket()
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
    configure_logging(get_settings().log_format)
    app = FastAPI(title="Ruxsatnoma-urmon API", lifespan=lifespan)

    settings = get_settings()
    if settings.cors_origins:
        # Cross-origin adminka (ruling 3): credentials are cookies, so the origin list
        # must be explicit — "*" is invalid with allow_credentials.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-Id", "Idempotency-Key"],
            expose_headers=["X-Request-Id"],
            max_age=600,
        )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.correlation_id = rid
        structlog.contextvars.bind_contextvars(**{CORRELATION_ID_KEY: rid})
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars(CORRELATION_ID_KEY)
        response.headers["X-Request-Id"] = rid
        return response

    @app.middleware("http")
    async def cache_control_middleware(request: Request, call_next):
        # A 200 with no freshness information may be heuristically cached by a shared
        # cache/CDN in front of the API (RFC 9111 §4.2.2). /api/v1/* responses carry
        # session-scoped data (e.g. /auth/me's csrf_token), so every one of them must
        # opt out explicitly; /health is a cacheable, unauthenticated probe and stays
        # untouched.
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_body(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # Ловит и штатные fastapi.HTTPException (наследник), и роутинговые
        # 404/405, которые Starlette поднимает как starlette.exceptions.HTTPException.
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code)
        if code is not None:
            message = ERRORS[code][1]
        else:
            code = "ERR-SYS-001"
            message = exc.detail if isinstance(exc.detail, str) else ERRORS[code][1]
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(request, code, message, None),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body(
                request,
                "ERR-VAL-001",
                ERRORS["ERR-VAL-001"][1],
                {"errors": jsonable_encoder(exc.errors())},
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        # Текст исключения наружу не отдаём — только код; детали в логах.
        # ServerErrorMiddleware вызывает этот хендлер в обход correlation_middleware
        # (contextvar correlation_id к этому моменту уже отвязан) — id и заголовок
        # проставляем здесь явно из request.state.
        rid = getattr(request.state, "correlation_id", None)
        structlog.get_logger().exception("unhandled_error", correlation_id=rid)
        return JSONResponse(
            status_code=500,
            content=_error_body(request, "ERR-SYS-001", "Внутренняя ошибка сервера", None),
            headers={"X-Request-Id": rid} if rid else None,
        )

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(refs_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1")

    return app
