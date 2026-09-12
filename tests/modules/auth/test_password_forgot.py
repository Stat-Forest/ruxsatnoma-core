"""Self-service password reset (decision #208): lookup masks and hides, the
code goes to the card's own contact, the reset burns the code, revokes every
session and clears a lockout. An unknown login and a card with no contacts
answer alike.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.security import verify_password
from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Session, User
from tests.conftest import make_client
from tests.modules.auth.conftest import _delivered_code
from tests.modules.auth.test_login import PASSWORD, make_staff

API = "/api/v1"


def _phone() -> str:
    return f"+9989{uuid.uuid4().int % 10**8:08d}"


def _email() -> str:
    return f"forgot-{uuid.uuid4().hex[:10]}@test.uz"


async def test_lookup_masks_filled_contacts_and_hides_unknown_login(db):
    phone, email = _phone(), _email()
    user, _ = await make_staff(db, phone=phone, email=email)
    only_phone, _ = await make_staff(db, phone=phone)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(f"{API}/auth/password/forgot/lookup", json={"login": user.login})
        assert r.status_code == 200
        assert r.json() == {"phone": f"+998 ** *** ** {phone[-2:]}", "email": "f***@test.uz"}

        r = await client.post(
            f"{API}/auth/password/forgot/lookup", json={"login": only_phone.login}
        )
        assert r.json() == {"phone": f"+998 ** *** ** {phone[-2:]}", "email": None}

        r = await client.post(f"{API}/auth/password/forgot/lookup", json={"login": "nobody-here"})
        assert r.status_code == 200
        assert r.json() == {"phone": None, "email": None}

        # sending to a channel the card does not have is refused, uniformly
        r = await client.post(
            f"{API}/auth/password/forgot/send", json={"login": only_phone.login, "channel": "email"}
        )
        assert r.status_code == 401
        r = await client.post(
            f"{API}/auth/password/forgot/send", json={"login": "nobody-here", "channel": "phone"}
        )
        assert r.status_code == 401


async def test_full_reset_flow_revokes_sessions_and_clears_lockout(db, engine):
    user, _ = await make_staff(
        db,
        phone=_phone(),
        failed_login_count=4,
        locked_until=datetime.now(UTC) + timedelta(minutes=15),
    )
    db.add(
        Session(
            user_id=user.id,
            token_hash=uuid.uuid4().hex,
            csrf_token=uuid.uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    # Captured before the outbox drain: its commits expire the ORM row and a
    # lazy reload would then be attempted outside the greenlet.
    login, user_id = user.login, user.id
    await db.commit()
    new_password = "N3w!strong-pass"
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await client.post(
            f"{API}/auth/password/forgot/send", json={"login": login, "channel": "phone"}
        )
        assert r.status_code == 204
        code = await _delivered_code(db)

        # a weak password is refused BEFORE the code is spent
        r = await client.post(
            f"{API}/auth/password/forgot/reset",
            json={"login": login, "channel": "phone", "code": code, "new_password": "short"},
        )
        assert r.status_code == 422  # policy violation, not a spent code

        r = await client.post(
            f"{API}/auth/password/forgot/reset",
            json={
                "login": login,
                "channel": "phone",
                "code": "000000",
                "new_password": new_password,
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "ERR-AUTH-010"

        r = await client.post(
            f"{API}/auth/password/forgot/reset",
            json={
                "login": login,
                "channel": "phone",
                "code": code,
                "new_password": new_password,
            },
        )
        assert r.status_code == 204

        # the code is single-use
        r = await client.post(
            f"{API}/auth/password/forgot/reset",
            json={
                "login": login,
                "channel": "phone",
                "code": code,
                "new_password": new_password,
            },
        )
        assert r.status_code == 400

        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        assert verify_password(new_password, user.password_hash)
        assert user.failed_login_count == 0 and user.locked_until is None
        assert user.must_change_password is False
        db.expunge(user)

        # the new password logs in, the old one does not, the lockout is gone
        r = await client.post(f"{API}/auth/login", json={"login": login, "password": PASSWORD})
        assert r.status_code == 401
        r = await client.post(f"{API}/auth/login", json={"login": login, "password": new_password})
        assert r.status_code == 200

    stale = (
        (
            await db.execute(
                select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    # the pre-existing session is gone; the password step above minted none
    # of its own (MFA is on in tests, so a session appears only after the code)
    assert stale == []
    actions = (
        (
            await db.execute(
                select(AuditLog.action).where(
                    AuditLog.user_id == user_id, AuditLog.action == "user.password_reset_self"
                )
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["user.password_reset_self"]
