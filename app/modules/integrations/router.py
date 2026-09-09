"""E-IMZO proxy routes the browser needs (plan 05.2, task 7, rulings R2/R5).

The E-IMZO server must never be reachable from the internet (R2): Task 8's
compose service keeps it on the stack's PRIVATE network only, and these are
the whole of what a browser genuinely needs from it -- proxied through our
own API, which is the thing that IS reachable.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.ratelimit import rate_limit
from app.modules.auth.deps import get_current_user, require_permission
from app.modules.auth.models import User
from app.modules.integrations import service
from app.modules.integrations.permissions import EIMZO_HEALTH

router = APIRouter(prefix="/eimzo", tags=["integrations"])


class EimzoTimestampIn(BaseModel):
    pkcs7: str


class EimzoTimestampOut(BaseModel):
    pkcs7: str


@router.post(
    "/timestamp",
    response_model=EimzoTimestampOut,
    # Copies `/auth/eimzo/challenge`'s own rate limit dependency verbatim
    # (task brief) -- the SAME bucket and setting, not a second mechanism.
    dependencies=[Depends(rate_limit("eimzo_challenge", "ratelimit_challenge_per_minute"))],
)
async def eimzo_timestamp(
    body: EimzoTimestampIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> EimzoTimestampOut:
    """Attaches a trusted timestamp to an already-produced PKCS#7 signature
    (plan ruling R5): without one, the only evidence of WHEN a document was
    signed is the signer's own computer clock, and a permit is a legal
    document with a validity period. Any signed-in caller may reach this --
    it attaches no meaning to the document, only a time -- so no permission
    code gates it beyond being authenticated at all.

    A provider refusal surfaces as its own registered `ERR-INT-001`/
    `ERR-INT-002` (503/502), never a 500, with the provider's own machine-
    readable `provider_status`/`reason` in `error.details` when it answered
    at all (stage 3.8 ruling 9).
    """
    ip = request.client.host if request.client else None
    stamped = await service.attach_eimzo_timestamp(db, pkcs7=body.pkcs7, ip=ip)
    return EimzoTimestampOut(pkcs7=stamped)


@router.get("/health")
async def eimzo_health(
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[User, Depends(require_permission(EIMZO_HEALTH))],
) -> dict[str, Any]:
    """`sys_admin` only. `EIMZO_HEALTH` is registered but granted to nobody
    (`integrations.permissions`'s own docstring) -- the superuser bypass in
    `auth.deps._authorize` (decision #41 ruling 2) is what actually gates
    this, the same shape `applications.assign` uses.

    Proxies `/ping` and `/info` so an administrator can see whether the VPN
    is up and when the key expires, without shell access to the server. A
    provider outage surfaces as `ERR-INT-001`/`ERR-INT-002`, never a 500 --
    an administrator checking this route needs to see the real failure.
    """
    return await service.eimzo_health(db)
