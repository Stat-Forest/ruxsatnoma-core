"""Сборка приложения: lifespan (БД), обработчики ошибок; роутеры модулей — этап 3."""

import asyncio
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
from app.core.idempotency import StoredIdempotentResponse
from app.core.logging import CORRELATION_ID_KEY, configure_logging
from app.db import make_engine, make_session_factory
from app.event_subscriptions import register_event_subscriptions
from app.files_router import router as files_router
from app.modules.admin.announcements_router import admin_router as announcements_admin_router
from app.modules.admin.announcements_router import router as announcements_router
from app.modules.admin.integrations_router import router as integrations_admin_router
from app.modules.admin.refs_router import router as refs_router
from app.modules.admin.router import router as admin_router
from app.modules.admin.users_router import router as users_router
from app.modules.applications.router import router as applications_router
from app.modules.auth.router import router as auth_router
from app.modules.gis.imports_router import router as gis_imports_router
from app.modules.gis.layers_router import router as gis_layers_router
from app.modules.gis.router import router as gis_router
from app.modules.norms.calc_router import router as norms_calc_router
from app.modules.norms.refs_router import router as norms_refs_router
from app.modules.norms.router import router as norms_router
from app.modules.notifications.router import router as notifications_router
from app.modules.notifications.templates_router import router as notification_templates_router
from app.modules.notifications.webhooks_router import router as notifications_webhooks_router
from app.modules.payments.backoffice_router import router as payments_backoffice_router
from app.modules.payments.payme_router import router as payme_router
from app.modules.payments.refunds_router import router as refunds_router
from app.modules.payments.router import router as payments_router
from app.modules.permits.public_router import router as permits_public_router
from app.modules.permits.router import router as permits_router
from app.modules.signatures.router import router as signatures_router

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
    workers_stop: asyncio.Event | None = None
    workers_task: asyncio.Task[None] | None = None
    if settings.workers_mode == "embedded":
        from app.workers.runner import run_all

        workers_stop = asyncio.Event()
        workers_task = asyncio.create_task(
            run_all(app.state.engine, app.state.session_factory, stop=workers_stop)
        )
    yield
    try:
        if workers_stop is not None and workers_task is not None:
            workers_stop.set()
            await workers_task
    finally:
        # dispose() must run even if awaiting workers_task raises (review
        # finding 2) — otherwise a stuck/failed worker shutdown leaks the
        # engine's whole connection pool instead of just failing loudly.
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
    settings = get_settings()
    configure_logging(settings.log_format)
    register_event_subscriptions()
    # API docs/schema are a dev convenience, not something to expose in test/prod
    # (stage 3.3b): app_env=dev is the only state that turns them on.
    docs_enabled = settings.app_env == "dev"
    app = FastAPI(
        title="Ruxsatnoma-urmon API",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

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

    @app.exception_handler(StoredIdempotentResponse)
    async def stored_idempotent_handler(request: Request, exc: StoredIdempotentResponse):
        return JSONResponse(status_code=exc.status_code, content=exc.body)

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
    app.include_router(announcements_router, prefix="/api/v1")
    app.include_router(announcements_admin_router, prefix="/api/v1")
    app.include_router(integrations_admin_router, prefix="/api/v1")
    app.include_router(notification_templates_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(notifications_webhooks_router, prefix="/api/v1")
    app.include_router(files_router, prefix="/api/v1")
    app.include_router(gis_layers_router, prefix="/api/v1")
    app.include_router(gis_imports_router, prefix="/api/v1")
    app.include_router(gis_router, prefix="/api/v1")
    app.include_router(norms_refs_router, prefix="/api/v1")
    app.include_router(norms_router, prefix="/api/v1")
    app.include_router(norms_calc_router, prefix="/api/v1")
    app.include_router(signatures_router, prefix="/api/v1")
    app.include_router(applications_router, prefix="/api/v1")
    app.include_router(payments_router, prefix="/api/v1")
    app.include_router(payments_backoffice_router, prefix="/api/v1")
    app.include_router(refunds_router, prefix="/api/v1")
    app.include_router(payme_router, prefix="/api/v1")
    app.include_router(permits_router, prefix="/api/v1")
    # The anonymous QR check (С12). Under `permits` and not a `public` module,
    # which is level 5 and stage 4.6 — plan 03.11a ruling 15; the PATH is
    # `design/03`'s own, so 4.6 inherits a working route rather than a rival.
    app.include_router(permits_public_router, prefix="/api/v1")

    return app
