"""Provider delivery reports. Anonymous by nature — Eskiz posts here — so the
shared secret in the path is the whole authentication (plan 03.5 ruling 18)."""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_db
from app.core.errors import err
from app.core.ratelimit import rate_limit
from app.modules.notifications import service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _body(request: Request) -> dict[str, Any]:
    """Eskiz's reports have arrived as JSON and as form posts over the years; accept
    both, and treat anything else as an empty body (→ dead letter downstream)."""
    try:
        payload = await request.json()
    except Exception:
        try:
            payload = dict(await request.form())
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


@router.post(
    "/eskiz/{secret}",
    dependencies=[Depends(rate_limit("webhook", "ratelimit_webhook_per_minute"))],
)
async def eskiz_delivery_report(
    secret: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    expected = get_settings().eskiz_callback_secret
    # 404, not 403: an anonymous caller must not learn that the path is real.
    if not expected or not secrets.compare_digest(secret, expected):
        raise err("ERR-SYS-003", details={})
    return {"result": await service.apply_delivery_report(db, await _body(request))}
