"""The request's transaction must be committed BEFORE its response is sent.

FastAPI (>= 0.106) runs a `yield` dependency's teardown AFTER the response has
been handed to the server, so a `commit()` placed there opens a window in which
the client holds a 200 — and its `Set-Cookie` — while the write it answers for is
still invisible to every other connection and to the client's own next request.

Seen on the dev stand 2026-09-09: `POST /auth/login` answered 200 with a fresh
session cookie, and a request fired ~6 ms later came back `ERR-AUTH-002`. The
adminka reads that code as "session gone" (ruling 10), clears the session and
bounces to /login — so a user who signs in is thrown straight back out with
"Сессия истекла". The window closed by ~40 ms, which is why the same login
succeeds on the next try and why nothing in the suite ever saw it.

**Assert the ORDER, not what another connection can see.** A probe that opens a
second connection has to `await`, and that await is itself the event loop's
chance to finish the very commit the probe is trying to catch — it reports the
race as absent on code that has it. The two `order.append` calls below are
synchronous, so nothing can interleave between them.
"""

import httpx
import pyotp
from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession

from app.main import create_app
from tests.modules.auth.test_login import PASSWORD, make_staff

API = "/api/v1"


async def test_the_transaction_commits_before_the_response_headers_are_sent(db):
    user, secret = await make_staff(db)
    await db.commit()
    app = create_app()
    order: list[str] = []

    @event.listens_for(OrmSession, "after_commit")
    def _record_commit(session: OrmSession) -> None:
        order.append("commit")

    def _record_response_start(scope, receive, send):
        async def wrapped(scope, receive, send):
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    order.append(f"response.start({message['status']})")
                await send(message)

            await app(scope, receive, send_wrapper)

        return wrapped(scope, receive, send)

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=_record_response_start)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                r1 = await client.post(
                    f"{API}/auth/login", json={"login": user.login, "password": PASSWORD}
                )
                assert r1.status_code == 200
                order.clear()  # the login step's own events are not what this asserts
                r2 = await client.post(
                    f"{API}/auth/mfa/verify",
                    json={"mfa_token": r1.json()["mfa_token"], "code": pyotp.TOTP(secret).now()},
                )
    finally:
        event.remove(OrmSession, "after_commit", _record_commit)

    assert r2.status_code == 200
    assert order == ["commit", "response.start(200)"], (
        "the session row must be committed before the client can act on the response; "
        f"actual order was {order}"
    )
