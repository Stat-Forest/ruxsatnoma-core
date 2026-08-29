"""Every query and every PostGIS predicate of the gis module. Geometry never
travels through Python: the repo builds SQL, PostGIS evaluates it."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.gis.models import GisLayer


async def list_layers(db: AsyncSession) -> list[GisLayer]:
    result = await db.execute(select(GisLayer).order_by(GisLayer.code))
    return list(result.scalars().all())


async def layer_by_code(db: AsyncSession, code: str) -> GisLayer | None:
    result = await db.execute(select(GisLayer).where(GisLayer.code == code))
    return result.scalar_one_or_none()
