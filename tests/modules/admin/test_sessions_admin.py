"""/admin/users/{id}/sessions, /admin/sessions/{id}/revoke,
/admin/users/{id}/sessions/revoke-all, /admin/users/stats (С23, Task 7): session
administration and user counters."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import SESSIONS_REVOKE_ANY, USERS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


async def test_list_sessions_shows_only_live(db):
    _, token, csrf = await signed_in_with(db, SESSIONS_REVOKE_ANY)
    target = await make_user(db)
    live, _, _ = await make_session(db, target)
    await make_session(db, target, revoked_at=datetime.now(UTC))
    await make_session(db, target, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{target.id}/sessions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {row["id"] for row in body} == {str(live.id)}
    assert set(body[0]) == {"id", "created_at", "last_seen_at", "expires_at", "ip", "user_agent"}


async def test_list_sessions_requires_permission(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    target = await make_user(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{target.id}/sessions")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_revoke_session_logs_out_and_audits(db):
    admin, token, csrf = await signed_in_with(db, SESSIONS_REVOKE_ANY)
    target = await make_user(db)
    session_row, target_token, _ = await make_session(db, target)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/sessions/{session_row.id}/revoke")
    assert r.status_code == 204, r.text

    async with make_client(app, lifespan=True) as client2:
        client2.cookies.set("session", target_token)
        me = await client2.get(f"{API}/auth/me")
    assert me.status_code == 401

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "session.revoke", AuditLog.object_id == session_row.id
            )
        )
    ).scalar_one()
    assert entry.user_id == admin.id
    assert entry.object_type == "session"


async def test_revoke_unknown_session_is_404(db):
    _, token, csrf = await signed_in_with(db, SESSIONS_REVOKE_ANY)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/sessions/{uuid.uuid4()}/revoke")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_revoke_all_sessions_returns_count_and_logs_out_all(db):
    _, token, csrf = await signed_in_with(db, SESSIONS_REVOKE_ANY)
    target = await make_user(db)
    _, token_a, _ = await make_session(db, target)
    _, token_b, _ = await make_session(db, target)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/sessions/revoke-all")
    assert r.status_code == 200, r.text
    assert r.json() == {"revoked": 2}

    for t in (token_a, token_b):
        async with make_client(app, lifespan=True) as client2:
            client2.cookies.set("session", t)
            me = await client2.get(f"{API}/auth/me")
        assert me.status_code == 401

    entries = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "session.revoke", AuditLog.object_id == target.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1  # one audit row for the whole action, not N per-session
    assert entries[0].object_type == "user"
    assert entries[0].new_value == {"revoked": 2}


async def test_revoke_all_unknown_user_is_404(db):
    _, token, csrf = await signed_in_with(db, SESSIONS_REVOKE_ANY)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{uuid.uuid4()}/sessions/revoke-all")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_stats_requires_view_or_manage(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/stats")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_stats_counts_match_delta(db):
    """The shared test DB carries rows from every other test, so only a
    before/after delta inside this one test is meaningful — never an absolute
    count."""
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        before = (await client.get(f"{API}/admin/users/stats")).json()

        user_a = await make_user(db, role_code="executor_staff", status="active")
        user_b = await make_user(db, role_code="gis_specialist", status="blocked")
        await make_session(db, user_a)
        await make_session(db, user_a)
        await db.commit()

        after = (await client.get(f"{API}/admin/users/stats")).json()

    assert user_a.id and user_b.id  # sanity: both fixtures persisted
    assert after["total"] - before["total"] == 2
    assert after["by_status"].get("active", 0) - before["by_status"].get("active", 0) == 1
    assert after["by_status"].get("blocked", 0) - before["by_status"].get("blocked", 0) == 1
    assert (
        after["by_role"].get("executor_staff", 0) - before["by_role"].get("executor_staff", 0) == 1
    )
    assert (
        after["by_role"].get("gis_specialist", 0) - before["by_role"].get("gis_specialist", 0) == 1
    )
    assert after["active_sessions"] - before["active_sessions"] == 2
