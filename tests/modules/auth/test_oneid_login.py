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


class _TokenIssuingOneId:
    """A stand-in for the live adapter: the mock never has an access token,
    because it never talks to a provider that could issue one."""

    def __init__(self, token: str = "tok-live-1") -> None:
        self.token = token

    def authorize_url(self, *, state: str, redirect_uri: str, scope: str) -> str:
        return f"{redirect_uri}?code=x&state={state}"

    async def exchange_code(self, code: str):
        from app.modules.integrations.adapters.oneid import OneIdLogin, OneIdProfile

        return OneIdLogin(
            profile=OneIdProfile(pinfl=code, full_name="TOKEN HOLDER"),
            access_token=self.token,
        )

    async def logout(self, access_token: str | None) -> None:
        return None


async def test_the_session_keeps_the_access_token_for_one_log_out(db, monkeypatch):
    """Stage 5.1 task 4: without it our logout closes only our own session and
    the OneID session survives in the browser — on a shared computer the next
    person's "sign in with OneID" lands in this citizen's cabinet."""
    from app.modules.auth import service

    monkeypatch.setattr(service, "get_oneid_adapter", lambda: _TokenIssuingOneId())
    pinfl = unique_pinfl()
    _user, row, _token, _csrf = await service.login_via_oneid(
        db, code=pinfl, ip=None, user_agent=None
    )
    assert row.oneid_access_token == "tok-live-1"
    # And it stayed out of the profile snapshot, which IS partly returned to
    # the browser and read back for the director_registry basis.
    assert "tok-live-1" not in str(_user.oneid_profile)


async def test_a_mock_login_stores_no_token(db):
    from app.modules.auth import service

    _user, row, _t, _c = await service.login_via_oneid(
        db, code=encode_mock_code(profile(unique_pinfl())), ip=None, user_agent=None
    )
    assert row.oneid_access_token is None


async def test_the_access_token_is_in_no_response_body(db, engine, monkeypatch):
    """`/auth/me` returns everything else about the session; a bearer token for
    a state system must not be among it."""
    from app.modules.auth import service

    monkeypatch.setattr(service, "get_oneid_adapter", lambda: _TokenIssuingOneId())
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.get(f"{API}/auth/oneid/authorize")
        state = client.cookies.get("oneid_state")
        assert r1.status_code == 200 and state
        r = await client.get(
            f"{API}/auth/oneid/callback", params={"code": unique_pinfl(), "state": state}
        )
        assert r.status_code == 303
        me = await client.get(f"{API}/auth/me")
        assert me.status_code == 200
        assert "tok-live-1" not in me.text
        assert "oneid_access_token" not in me.text


class _SpyLogoutOneId(_TokenIssuingOneId):
    def __init__(self) -> None:
        super().__init__()
        self.logged_out: list[str | None] = []

    async def logout(self, access_token: str | None) -> None:
        self.logged_out.append(access_token)


async def test_logout_ends_the_oneid_session_and_clears_the_token(db, monkeypatch):
    from app.modules.auth import service

    spy = _SpyLogoutOneId()
    monkeypatch.setattr(service, "get_oneid_adapter", lambda: spy)
    _user, row, _t, _c = await service.login_via_oneid(
        db, code=unique_pinfl(), ip=None, user_agent=None
    )
    await service.logout_session(db, row)
    assert spy.logged_out == ["tok-live-1"]
    assert row.revoked_at is not None
    assert row.oneid_access_token is None  # nothing left to replay


async def test_a_provider_failure_still_logs_the_citizen_out(db, monkeypatch):
    """Our own revocation happens FIRST and unconditionally: a citizen who
    pressed "sign out" must be signed out of our system whatever OneID does,
    and the provider call may hang for the adapter's whole timeout."""
    from app.modules.auth import service
    from app.modules.integrations.adapters.oneid import OneIdError

    class _Broken(_TokenIssuingOneId):
        async def logout(self, access_token: str | None) -> None:
            raise OneIdError("ERR-INT-001")

    monkeypatch.setattr(service, "get_oneid_adapter", lambda: _Broken())
    _user, row, _t, _c = await service.login_via_oneid(
        db, code=unique_pinfl(), ip=None, user_agent=None
    )
    await service.logout_session(db, row)  # must not raise
    assert row.revoked_at is not None
    assert row.oneid_access_token is None


async def test_logout_of_a_session_with_no_oneid_token_calls_nothing(db, monkeypatch):
    """A password or E-IMZO session — and every session opened before this
    stage — has no token, and must not reach the provider at all."""
    from app.modules.auth import service

    # The real mock adapter, so this is a genuine tokenless OneID session.
    _user, row, _t, _c = await service.login_via_oneid(
        db, code=encode_mock_code(profile(unique_pinfl())), ip=None, user_agent=None
    )
    assert row.oneid_access_token is None

    spy = _SpyLogoutOneId()
    monkeypatch.setattr(service, "get_oneid_adapter", lambda: spy)
    await service.logout_session(db, row)
    assert spy.logged_out == []
    assert row.revoked_at is not None


async def test_the_logout_route_goes_through_logout_session(db, engine, monkeypatch):
    """The route, not just the service: a password session must log out
    exactly as before, and an OneID one must reach the provider."""
    from app.modules.auth import service

    spy = _SpyLogoutOneId()
    monkeypatch.setattr(service, "get_oneid_adapter", lambda: spy)
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        r1 = await client.get(f"{API}/auth/oneid/authorize")
        state = client.cookies.get("oneid_state")
        assert r1.status_code == 200 and state
        assert (
            await client.get(
                f"{API}/auth/oneid/callback", params={"code": unique_pinfl(), "state": state}
            )
        ).status_code == 303
        out = await client.post(
            f"{API}/auth/logout", headers={"X-CSRF-Token": client.cookies.get("csrf_token") or ""}
        )
        assert out.status_code in (200, 204)
    assert spy.logged_out == ["tok-live-1"]
