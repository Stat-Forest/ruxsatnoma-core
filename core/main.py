"""FastAPI application factory for the core service."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.api.middleware import CorrelationIdMiddleware
from core.config import get_settings
from core.db import dispose_engine

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold infrastructure resources for the lifetime of the application."""
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Build the application: middleware, service routes and module routers."""
    settings = get_settings()
    app = FastAPI(
        title="Ruxsatnoma core",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health", tags=["service"])
    async def health() -> dict[str, str]:
        """Liveness probe: reports the process, not its dependencies."""
        return {
            "status": "ok",
            "service": settings.app_name,
            "environment": settings.environment,
        }

    # Module routers are registered here by their owners, one line per module:
    #     from core.modules import geo
    #     app.include_router(geo.router, prefix=API_PREFIX)

    return app


app = create_app()
