"""OneID login: state double-submit, auto-created applicants, role-agnostic entry."""

import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.auth.models import Role, User
from app.modules.integrations.adapters.oneid import OneIdLegalInfo, OneIdProfile, encode_mock_code
from tests.conftest import make_client

API = "/api/v1"


def profile(pinfl: str, **overrides) -> OneIdProfile:
    defaults = dict(pinfl=pinfl, full_name="ONEID USER", phone="+998900000001")
    return OneIdProfile(**{**defaults, **overrides})


def unique_pinfl() -> str:
    return f"3{uuid.uuid4().int % 10**13:013d}"


async def oneid_login(client, prof: OneIdProfile):
    r1 = await client.get(f"{API}/auth/oneid/authorize")
    assert r1.status_code == 200 and "redirect_url" in r1.json()
    state = client.cookies.get("oneid_state")
    assert state
    return await client.get(
        f"{API}/auth/oneid/callback", params={"code": encode_mock_code(prof), "state": state}
    )


async def test_first_login_creates_applicant_user(db, engine):
    pinfl = unique_pinfl()
    prof = profile(pinfl, legal_info=(OneIdLegalInfo(le_tin="111222333", le_name="OOO L"),))
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await oneid_login(client, prof)
        assert r.status_code == 303
        assert "session" in r.cookies and "csrf_token" in r.cookies
        me = (await client.get(f"{API}/auth/me")).json()
        assert me["user"]["full_name"] == "ONEID USER"
        assert me["role"]["code"] == "applicant"
    from app.db import make_session_factory

    async with make_session_factory(engine)() as fresh:
        user = (await fresh.execute(select(User).where(User.pinfl == pinfl))).scalar_one()
        assert user.oneid_profile is not None
        assert user.oneid_profile["legal_info"][0]["le_tin"] == "111222333"
        assert user.password_hash is None and user.last_login_at is not None


async def test_second_login_reuses_user(db, engine):
    pinfl = unique_pinfl()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        assert (await oneid_login(client, profile(pinfl))).status_code == 303
    async with make_client(app, lifespan=True) as client:
        assert (await oneid_login(client, profile(pinfl))).status_code == 303
    from app.db import make_session_factory

    async with make_session_factory(engine)() as fresh:
        count = len(
            (await fresh.execute(select(User.id).where(User.pinfl == pinfl))).scalars().all()
        )
    assert count == 1


async def test_prosecutor_logs_in_with_own_role(db):
    pinfl = unique_pinfl()
    role_id = (await db.execute(select(Role.id).where(Role.code == "prosecutor"))).scalar_one()
    db.add(User(full_name="Prosecutor", role_id=role_id, pinfl=pinfl))
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await oneid_login(client, profile(pinfl, full_name="PROSECUTOR X"))
        assert r.status_code == 303
        me = await client.get(f"{API}/auth/me")
    assert me.json()["role"]["code"] == "prosecutor"


async def test_blocked_user_denied(db):
    pinfl = unique_pinfl()
    role_id = (await db.execute(select(Role.id).where(Role.code == "applicant"))).scalar_one()
    db.add(User(full_name="Blocked", role_id=role_id, pinfl=pinfl, status="blocked"))
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await oneid_login(client, profile(pinfl))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ERR-AUTH-001"


async def test_state_mismatch_redirects_to_login_not_a_json_error(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.get(f"{API}/auth/oneid/authorize")
        r = await client.get(
            f"{API}/auth/oneid/callback",
            params={"code": encode_mock_code(profile(unique_pinfl())), "state": "forged"},
        )
    assert r.status_code == 303
    assert r.headers["location"] == "http://localhost:5173/login?error=oneid"
    assert "session" not in r.cookies


async def test_callback_redirects_to_the_adminka_with_session_cookies(db, engine, monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_BASE_URL", "https://admin.example.uz")
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r = await oneid_login(client, profile(unique_pinfl()))
        assert r.status_code == 303
        assert r.headers["location"] == "https://admin.example.uz/auth/oneid/return"
        assert "session" in r.cookies and "csrf_token" in r.cookies
        # The cookies are real, not decoration: the session they carry works.
        me = await client.get(f"{API}/auth/me")
        assert me.status_code == 200
        assert me.json()["role"]["code"] == "applicant"
    get_settings.cache_clear()


async def test_callback_with_a_bad_state_redirects_to_login_not_a_json_error(db, monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ADMIN_BASE_URL", "https://admin.example.uz")
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.get(f"{API}/auth/oneid/authorize")
        r = await client.get(
            f"{API}/auth/oneid/callback",
            params={"code": encode_mock_code(profile(unique_pinfl())), "state": "not-the-state"},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "https://admin.example.uz/login?error=oneid"
        assert "session" not in r.cookies
    get_settings.cache_clear()


async def test_provider_error_maps_to_502(db):
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        await client.get(f"{API}/auth/oneid/authorize")
        state = client.cookies.get("oneid_state")
        r = await client.get(
            f"{API}/auth/oneid/callback", params={"code": "broken", "state": state}
        )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "ERR-INT-002"
