"""Healthcheck: liveness без зависимостей, readiness с проверкой БД/PostGIS."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.errors import err

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(db: AsyncSession = Depends(get_db)) -> dict:
    try:
        version = (await db.execute(text("SELECT PostGIS_Version()"))).scalar_one()
    except Exception as exc:  # БД недоступна/без PostGIS
        raise err("ERR-SYS-002") from exc
    return {"status": "ok", "postgis": version}
