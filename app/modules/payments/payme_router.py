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

Commit discipline (why `_process` below ends most of its branches with an
explicit `db.commit()` or `db.rollback()` rather than trusting `get_db`'s
own commit-on-success): a `payme.PaymeError` can carry a legitimate
mutation that must survive it — the 12h-timeout auto-cancel (ruling 4) is
exactly `-31008` WITH a state change that has to reach the database, or
`CheckTransaction` right after would not see `reason: 4` — so that branch
commits explicitly before returning. An UNEXPECTED exception is the
opposite: whatever the failed statement left pending must never reach the
database, so that branch rolls back explicitly before returning.

Whole-branch review finding I1: those explicit `db.commit()`/`db.rollback()`
calls can themselves raise (a genuine DB-level failure at exactly that
moment) — and until `payme_rpc` below wrapped `_process` in its own outer
`try`, such a failure would leave `_process`, past `get_db`
(`app/core/deps.py`), into `app.main`'s generic handler as a real 500, the
one outcome this whole route exists to prevent. `payme_rpc` is the
backstop that makes this module docstring's "never lets ANY exception
escape" literally true, independent of what `_process` itself does or
fails to do.

That backstop makes ONE call on `db` of its own, and only one: a best-effort
`rollback()` wrapped in its own `try` (whole-branch review). Answering 200
from the outer guard is not the end of the request — `get_db`
(`app/core/deps.py`) then takes its own success branch and commits the
session a SECOND time, and if `_process`'s own commit failed in a way that
left the transaction needing an explicit rollback, that second commit raises
outside this route entirely and renders a real 500. Rolling back first
leaves `get_db` a clean transaction to commit; the nested `try` is what
stops a failing rollback (a dropped connection) from reopening the very hole
it is closing. Nothing is lost by it: this branch is only ever reached after
`_process` has already decided the request failed.
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


async def _process(request: Request, db: AsyncSession) -> JSONResponse:
    """Everything `payme_rpc` used to do directly (unchanged): parse the
    envelope, verify credentials, dispatch, respond. Split out so
    `payme_rpc` can wrap ALL of it — envelope parsing included — in one
    outer guard (review finding I1) without that guard itself needing to
    know which branch below is currently running."""
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
        await get_payme_adapter().verify(db, request.headers.get("Authorization"))
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


@router.post("/payme")
async def payme_rpc(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> JSONResponse:
    try:
        return await _process(request, db)
    except Exception as exc:
        # Review finding I1's own backstop — see the module docstring's
        # "Commit discipline" section for the full reasoning. Reachable only
        # when one of `_process`'s own `db.commit()`/`db.rollback()` calls
        # itself raises; every OTHER failure is already handled, and
        # answered 200, inside `_process`. `rpc_id` is unknown at this
        # level by design (re-parsing the body here to recover it would
        # itself be one more thing that can fail) — `null` is valid
        # JSON-RPC for a response whose request could not be identified,
        # the same convention `-32700`/`-32600` above already use.
        try:
            # The residual hole the re-reviewer left for the final review:
            # once we answer 200 here, `get_db` (`app/core/deps.py`) takes
            # its own `else:` branch and calls `session.commit()` a SECOND
            # time — and if the failure above left the session needing an
            # explicit rollback, THAT commit raises past this route
            # entirely and renders a real 500, the one outcome this module
            # exists to prevent. Rolling back first leaves `get_db` a clean
            # transaction to commit. Nested, because a rollback on a
            # already-dead connection raises too, and reintroducing the
            # same hole while closing it would be absurd.
            await db.rollback()
        except Exception:
            pass
        logger.error("payme_rpc_commit_failed", error=repr(exc))
        return _error(None, payme.ERR_INTERNAL, "Internal error")
