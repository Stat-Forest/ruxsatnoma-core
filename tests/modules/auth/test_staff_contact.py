"""Decision #150: staff get no SMS at all, so they change their phone with no code.

Two halves of one rule. Nothing is ever sent to an employee over SMS — the
notification they would pay for is already in the cabinet they have open — so
requiring an SMS code to change their own number would be a confirmation they
cannot receive. An applicant still verifies: their phone IS the channel.
"""

from datetime import UTC, datetime

from app.main import create_app
from app.modules.auth.models import Applicant, User
from tests.conftest import make_client
from tests.modules.auth.conftest import _delivered_code
from tests.modules.auth.test_otp import unique_phone
from tests.modules.auth.test_registration import unique_pinfl
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


def csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


async def _registered_applicant(db, **overrides) -> User:
    """An applicant WITH their own `applicants` row. Without it every route but the
    registration-exempt ones answers ERR-AUTH-008, and a 403 would hide whichever
    behaviour the test is actually about."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl(), **overrides)
    db.add(
        Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    )
    await db.flush()
    return user


async def _client_for(db, user):
    session, token, csrf = await make_session(db, user)
    await db.commit()
    return create_app(), token, csrf


async def test_staff_change_their_phone_without_an_sms_code(db):
    user = await make_user(db, role_code="executor_staff")
    # Read the id BEFORE `_client_for` commits: the commit expires the instance, and
    # touching an attribute afterwards is a lazy refresh outside the greenlet.
    user_id = user.id
    app, token, csrf = await _client_for(db, user)
    phone = unique_phone()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        r = await client.patch(
            f"{API}/auth/me", json={"phone": phone}, headers={"X-CSRF-Token": csrf}
        )
    assert r.status_code == 200
    stored = await db.get(User, user_id)
    assert stored is not None
    await db.refresh(stored)
    assert stored.phone == phone
    # ...and it is recorded as UNVERIFIED: nobody checked that the number is theirs,
    # and stamping it verified would be a claim this path cannot support.
    assert stored.phone_verified_at is None


async def test_an_applicant_still_needs_the_code(db):
    """The other half. Without it this change would have quietly removed phone
    verification from the one role whose phone actually carries the permit."""
    user = await _registered_applicant(db, phone_verified_at=datetime.now(UTC))
    app, token, csrf = await _client_for(db, user)
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        r = await client.patch(
            f"{API}/auth/me", json={"phone": unique_phone()}, headers={"X-CSRF-Token": csrf}
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_an_applicant_with_a_code_is_verified_as_before(db):
    user = await _registered_applicant(db)
    user_id = user.id
    app, token, csrf = await _client_for(db, user)
    phone = unique_phone()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        r = await client.post(
            f"{API}/auth/otp/request",
            json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
        )
        assert r.status_code == 204
        r = await client.post(
            f"{API}/auth/otp/verify",
            json={"target": phone, "code": await _delivered_code(db), "purpose": "phone_verify"},
        )
        otp_token = r.json()["otp_token"]
        r = await client.patch(
            f"{API}/auth/me",
            json={"phone": phone, "otp_token": otp_token},
            headers={"X-CSRF-Token": csrf},
        )
    assert r.status_code == 200
    stored = await db.get(User, user_id)
    assert stored is not None
    await db.refresh(stored)
    assert stored.phone == phone
    assert stored.phone_verified_at is not None
