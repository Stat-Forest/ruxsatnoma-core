"""The one route a citizen reaches with no account at all — С12's QR check.

**Why this file is in `permits` and not in a `public` module** (plan ruling 15):
`design/03` files the path under `public`, which is level 5 and stage 4.6 — a
module nobody has planned yet. The route reads permits and nothing else, and
making it wait would leave the QR printed on every issued permit pointing at a
404 for weeks. The PATH stays exactly as `design/03` writes it, so when 4.6
arrives it inherits a working route instead of a competing one.

Separate from `router.py` because nothing here is like anything there: no
`get_current_user`, no `require_permission`, and a rate limit instead of both.
Keeping the anonymous surface in its own file is what makes it reviewable as a
surface — one import away from a reader asking "what can the internet reach".
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.ratelimit import consume
from app.modules.permits import service
from app.modules.permits.schemas import PublicCheckCard, PublicCheckMiss

router = APIRouter(prefix="/public", tags=["public"])


# `response_model=` rather than a return annotation (the `auth` router's idiom for
# the same situation): the service's contract is a plain dict, and the union is
# what FastAPI validates it into. The two members discriminate on `found`, whose
# `Literal[True]`/`Literal[False]` make the match exact rather than lucky.
#
# No `dependencies=[Depends(rate_limit(...))]`, unlike every other limited route
# here: the scope depends on the REQUEST (review I1), and a dependency picks its
# scope at import time. Re-declaring the three query parameters on a dependency
# just to read them again would put their bounds in two places, so the limit is
# taken in the handler instead — before any lookup, and after nothing but
# `check_channel`, which does no IO.
@router.get(
    "/permits/check",
    response_model=PublicCheckCard | PublicCheckMiss,
)
async def check_permit(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    qr: Annotated[
        str | None, Query(max_length=service.QR_TOKEN_MAX_LENGTH, description="Printed QR token")
    ] = None,
    series: Annotated[str | None, Query(max_length=service.SERIES_MAX_LENGTH)] = None,
    number: Annotated[int | None, Query(ge=1, le=service.MAX_PERMIT_NUMBER)] = None,
) -> dict[str, Any]:
    """Verify a permit anonymously: `?qr=` from the printed symbol, or
    `?series=&number=` for a citizen holding a paper copy.

    **Always 200.** An unknown token, an unknown number and a permit that is not
    public yet all answer `{"found": false}` — one shape for both outcomes,
    because a 404 for the unknown and a 200 for the known is a permit-number
    oracle anyone could walk (ruling 8).

    422 `ERR-VAL-001` for a request that names no permit at all (neither `qr` nor
    both halves of `series`+`number`), and for one whose `number` is outside
    `bigint`. Neither leaks anything: both are facts about the request's shape,
    fixed and public, never about the data behind it.

    429 `ERR-SYS-006` on the per-IP bucket, and **the two channels have their
    own buckets and their own settings rows** (review I1). That limit is the
    whole security control here, and the two paths are not equally exposed:
    `qr_token` is unguessable, while `permit_counters` hands out
    `last_number + 1`, so series+number is a gapless space anyone can walk. One
    shared bucket meant the walkable path could not be tightened without also
    throttling the citizen scanning a printed code — and a scanner behind a NAT
    starved honest scans coming off that same egress address. CAPTCHA is the
    front end's, at stage 6.

    Behind a proxy, uvicorn needs `--proxy-headers`/`--forwarded-allow-ips`: the
    limiter keys on `request.client.host`, and without them every citizen in the
    country shares one bucket.
    """
    channel = service.check_channel(qr_token=qr, series=series, number=number)
    await consume(
        request,
        db,
        scope=f"public_check:{channel}",
        setting_key=service.CHANNEL_RATELIMITS[channel],
    )
    return await service.public_check(db, qr_token=qr, series=series, number=number)
