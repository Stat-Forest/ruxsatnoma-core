"""Business rules of the spatial core. Public surface for levels 3+ (norms 3.7,
applications 3.9): published_version(), list_contours(), run_checks()."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.modules.audit import service as audit
from app.modules.auth.models import User
from app.modules.gis import repo
from app.modules.gis.models import GisLayer


async def list_layers(db: AsyncSession) -> list[GisLayer]:
    """A pass-through today (task-2 review, finding 2): this read acquires a real
    rule inside this same stage — Task 8 must refuse a non-public layer's
    features to an applicant — and that rule belongs here, not in the router."""
    return await repo.list_layers(db)


async def update_layer(
    db: AsyncSession,
    code: str,
    *,
    actor: User,
    style: dict[str, Any] | None = None,
    is_public: bool | None = None,
    status: str | None = None,
) -> GisLayer:
    layer = await repo.layer_by_code(db, code)
    if layer is None:
        raise err("ERR-SYS-003")
    before = {"style": layer.style, "is_public": layer.is_public, "status": layer.status}
    if style is not None:
        layer.style = style
    if is_public is not None:
        layer.is_public = is_public
    if status is not None:
        layer.status = status
    await db.flush()
    # An in-place UPDATE leaves onupdate columns expired (lesson) — the response
    # serializes this row, so refresh before returning it.
    await db.refresh(layer)
    await audit.log(
        db,
        action="gis_layer.update",
        user_id=actor.id,
        object_type="gis_layer",
        object_id=layer.id,
        old_value=before,
        new_value={"style": layer.style, "is_public": layer.is_public, "status": layer.status},
    )
    return layer
