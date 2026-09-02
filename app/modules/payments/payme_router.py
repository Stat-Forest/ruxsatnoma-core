"""The Payme JSON-RPC server — the always-200 HTTP shell over `payme.py`
(design/02 § payments, plan `03.10a-payments-core` task 4,
`design/04-integrations.md` §3 end to end, ruling B).

Payme is the CLIENT here, not a webhook sender we authenticate by a shared
secret in the path: every response this route sends carries HTTP 200, with
the JSON-RPC `error` object doing the work a status code would do elsewhere
— a non-200 is read by Payme as ITS OWN internal error `-32400` (§3.1), so
this route must never let ANY exception escape FastAPI's own machinery,
malformed body included. `notifications/webhooks_router.py`'s `_body()` is
the closest existing precedent for "never 500 on a body we merely fail to
understand" — this route generalises the same discipline to a JSON-RPC
envelope instead of a provider webhook, and additionally must recover from
every documented Payme protocol failure (`payme.PaymeError`) the same way.

None of this codebase's usual conventions about error envelopes apply here
(`design/04` §6): no `err("ERR-...")`, no `correlation_id` body, no
`response_model` — this route builds its JSON-RPC response BY HAND, and
never raises past its own top-level `try`. Ruling C: `-32300` (non-POST) is
deliberately out of scope — the route is declared POST-only below and
Starlette's own 404/405 handling (rendered by `app.main`'s ordinary
`StarletteHTTPException` handler, in OUR envelope shape) covers everything
that is not a POST; Payme itself only ever POSTs.

`_now()` is the ONE clock this route reads (ruling G) — tests monkeypatch
THIS name directly, never `app.core.time`, which this route has no reason to
use at all: `payme.handle`'s `now` is a plain UTC instant, not a business
calendar day.

Commit discipline (why every branch below ends with an explicit `db.commit()`
or `db.rollback()` rather than trusting `get_db`'s commit-on-success): this
function NEVER raises past itself, so `get_db` (`app/core/deps.py`) always
takes its "no exception propagated" branch and calls `session.commit()` on
return, REGARDLESS of which branch below produced the response. A
`payme.PaymeError` can carry a legitimate mutation that must survive it — the
12h-timeout auto-cancel (ruling 4) is exactly `-31008` WITH a state change
that has to reach the database, or `CheckTransaction` right after would not
see `reason: 4` — so that branch commits explicitly before returning. An
UNEXPECTED exception is the opposite: whatever the failed statement left
pending must never reach the database, so that branch rolls back explicitly
before returning, which also protects `get_db`'s own final commit from
retrying a half-aborted transaction (a `PendingRollbackError` there would
escape as a real 500 — the one outcome this whole route exists to prevent).
Both explicit calls make `get_db`'s own commit a harmless no-op afterward.
"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.modules.integrations.adapters.payme import PaymeAuthError, get_payme_adapter
from app.modules.payments import payme

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["payme"])

# design/04 §3.6 — the two failures that occur before `payme.handle` is ever
# reachable, so they are not `payme.py`'s own `PaymeError` codes.
_ERR_PARSE = -32700
_ERR_INVALID_REQUEST = -32600


def _now() -> datetime:
    return datetime.now(UTC)


def _error(
    rpc_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "id": rpc_id, "error": error})


@router.post("/payme")
async def payme_rpc(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> JSONResponse:
    try:
        body = json.loads(await request.body())
    except Exception:
        return _error(None, _ERR_PARSE, "Parse error")
    if not isinstance(body, dict):
        return _error(None, _ERR_INVALID_REQUEST, "Invalid Request")

    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params")
    if not isinstance(method, str) or not method:
        return _error(rpc_id, _ERR_INVALID_REQUEST, "Invalid Request")
    if not isinstance(params, dict):
        params = {}

    # ONE try covers both the credential check and the dispatch (self-review
    # fix: two separate try blocks left `get_payme_adapter()`/`.verify(...)`
    # raising anything OTHER than `PaymeAuthError` uncaught — a future bug
    # there would have escaped this function, past `get_db`, into
    # `app.main`'s generic handler as a real 500, the one outcome this whole
    # route exists to prevent). `PaymeAuthError` and `payme.PaymeError` are
    # unrelated sibling exception types (neither is the other's subclass),
    # so their relative order below does not matter; both must still precede
    # the catch-all.
    try:
        get_payme_adapter().verify(request.headers.get("Authorization"))
        result = await payme.handle(db, method, params, now=_now())
    except PaymeAuthError:
        return _error(rpc_id, payme.ERR_INSUFFICIENT_PRIVILEGE, "Insufficient privileges")
    except payme.PaymeError as exc:
        # See the module docstring's "Commit discipline" note: a legitimate
        # mutation (e.g. the 12h-timeout auto-cancel) may already be staged
        # on `db` and must survive this error response.
        await db.commit()
        return _error(rpc_id, exc.code, exc.message, exc.data)
    except Exception as exc:
        await db.rollback()
        # `method` only — never `params`/`body`, which can carry account and
        # amount data (ruling B: never echo the request payload).
        logger.error("payme_rpc_unhandled", error=repr(exc), method=method)
        return _error(rpc_id, payme.ERR_INTERNAL, "Internal error")

    await db.commit()
    return JSONResponse(status_code=200, content={"jsonrpc": "2.0", "id": rpc_id, "result": result})
