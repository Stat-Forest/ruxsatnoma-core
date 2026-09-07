"""C2 registration: consents, phone OTP, gate on unregistered applicants, MeOut."""

import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.auth.models import User, UserConsent
from app.modules.integrations.adapters.oneid import OneIdProfile, encode_mock_code
from tests.conftest import make_client
from tests.modules.auth.conftest import _delivered_code
from tests.modules.auth.test_otp import unique_phone

API = "/api/v1"


def unique_pinfl() -> str:
    return f"5{uuid.uuid4().int % 10**13:013d}"


async def oneid_login(client, pinfl: str):
    await client.get(f"{API}/auth/oneid/authorize")
    state = client.cookies.get("oneid_state")
    prof = OneIdProfile(pinfl=pinfl, full_name="REG USER", phone="+998905550001")
    r = await client.get(
        f"{API}/auth/oneid/callback", params={"code": encode_mock_code(prof), "state": state}
    )
    assert r.status_code == 303
    return r


def csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


async def verified_phone_token(client, phone: str, *, db) -> str:
    r = await client.post(
        f"{API}/auth/otp/request",
        json={"target_type": "phone", "target": phone, "purpose": "phone_verify"},
    )
    assert r.status_code == 204
    r = await client.post(
        f"{API}/auth/otp/verify",
        json={"target": phone, "code": await _delivered_code(db), "purpose": "phone_verify"},
    )
    return r.json()["otp_token"]


def registration_body(phone: str, otp_token: str, **overrides):
    body = {
        "consents": {"privacy_policy": "1.0", "offer": "1.0"},
        "phone": phone,
        "otp_token": otp_token,
        "email": "reg@test.uz",
    }
    return {**body, **overrides}


async def test_full_registration_flow(db, engine):
    pinfl, phone = unique_pinfl(), unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, pinfl)
        me = await client.get(f"{API}/auth/me")
        assert me.json()["registration_complete"] is False
        token = await verified_phone_token(client, phone, db=db)
        r = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(phone, token),
            headers=csrf_headers(client),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["registration_complete"] is True
        assert body["applicant"]["kind"] == "individual"
        assert body["applicant"]["pinfl"] == pinfl
        assert body["applicant"]["verified_at"] is not None  # oneid snapshot present
        # Ruling #113: the address gate sits at SUBMISSION
        # (`applications.checks.missing_for_pricing`), not here — a citizen may
        # complete registration and sign in with no address on file at all.
        assert body["applicant"]["address"] is None
        me = await client.get(f"{API}/auth/me")
        assert me.json()["registration_complete"] is True
    from app.db import make_session_factory

    async with make_session_factory(engine)() as fresh:
        user = (await fresh.execute(select(User).where(User.pinfl == pinfl))).scalar_one()
        assert user.phone == phone and user.phone_verified_at is not None
        consents = (
            (await fresh.execute(select(UserConsent).where(UserConsent.user_id == user.id)))
            .scalars()
            .all()
        )
        assert {c.doc_type for c in consents} == {"privacy_policy", "offer"}


async def test_gate_blocks_unregistered_but_allows_refs(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, unique_pinfl())
        assert (await client.get(f"{API}/auth/me")).status_code == 200
        assert (await client.get(f"{API}/refs/regions")).status_code == 200
        blocked = await client.get(f"{API}/admin/settings")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "ERR-AUTH-008"


async def test_stale_consent_version_422(db):
    pinfl, phone = unique_pinfl(), unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, pinfl)
        token = await verified_phone_token(client, phone, db=db)
        r = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(
                phone, token, consents={"privacy_policy": "0.9", "offer": "1.0"}
            ),
            headers=csrf_headers(client),
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ERR-VAL-001"


async def test_otp_token_target_must_match_phone(db):
    pinfl = unique_pinfl()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, pinfl)
        token = await verified_phone_token(client, unique_phone(), db=db)
        r = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(unique_phone(), token),  # a different phone
            headers=csrf_headers(client),
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ERR-AUTH-010"


async def test_double_registration_409(db):
    pinfl, phone = unique_pinfl(), unique_phone()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, pinfl)
        token = await verified_phone_token(client, phone, db=db)
        first = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(phone, token),
            headers=csrf_headers(client),
        )
        assert first.status_code == 200
        token2 = await verified_phone_token(client, phone, db=db)
        r = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(phone, token2),
            headers=csrf_headers(client),
        )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ERR-AUTH-012"


async def test_patch_me_blocked_for_unregistered_applicant(db):
    """Finding 1 (final review): the registration-gate exemption is method-aware —
    PATCH /auth/me must not ride the GET-only /auth/me exemption. An unregistered
    applicant PATCHing /auth/me is gated to ERR-AUTH-008 before the request ever
    reaches OTP validation (otp_token below is a deliberately bogus value)."""
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await oneid_login(client, unique_pinfl())
        r = await client.patch(
            f"{API}/auth/me",
            json={"phone": unique_phone(), "otp_token": "bogus"},
            headers=csrf_headers(client),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "ERR-AUTH-008"
        # the GET exemption itself must still work for the same user
        assert (await client.get(f"{API}/auth/me")).status_code == 200


async def test_staff_cannot_register_as_applicant(db):
    """A staff role calling complete-registration gets 403, and staff are never gated."""
    from tests.modules.auth.test_sessions import make_session, make_user

    user = await make_user(db, role_code="executor_staff")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        client.cookies.set("csrf_token", csrf)
        assert (await client.get(f"{API}/auth/me")).json()["registration_complete"] is True
        r = await client.post(
            f"{API}/auth/complete-registration",
            json=registration_body(unique_phone(), "irrelevant"),
            headers={"X-CSRF-Token": csrf},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"
