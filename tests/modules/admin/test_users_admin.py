"""/admin/users: view vs manage, zone scoping, credentials handout, guards."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.crypto import decrypt_str, encrypt_str
from app.core.security import hash_password, validate_password_policy, verify_password
from app.main import create_app
from app.modules.admin.models import Organization
from app.modules.audit.models import AuditLog
from app.modules.auth.models import User
from app.modules.auth.permissions import USERS_MANAGE, USERS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


async def test_list_requires_view_or_manage(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_list_with_view_zone_scoped(db, agency):
    """A viewer whose own zone is orgA sees orgA's users, not orgB's, with no
    explicit filter — the restriction comes from `zone_of(actor)`, not the query."""
    suffix = uuid.uuid4().hex[:6]
    org_a = Organization(
        kind="leshoz", code=f"zva-{suffix}", name={"uz_cyrl": "А"}, parent_id=agency.id
    )
    org_b = Organization(
        kind="leshoz", code=f"zvb-{suffix}", name={"uz_cyrl": "Б"}, parent_id=agency.id
    )
    db.add_all([org_a, org_b])
    await db.flush()

    viewer, token, csrf = await signed_in_with(db, USERS_VIEW)
    viewer.organization_id = org_a.id
    user_a = await make_user(db, organization_id=org_a.id)
    user_b = await make_user(db, organization_id=org_b.id)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users")
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["items"]}
    assert str(user_a.id) in ids
    assert str(user_b.id) not in ids


async def test_list_with_manage_sees_all(db, agency):
    """A manage-holder whose own zone is orgA can still reach orgB explicitly —
    proving manage is NOT auto-restricted to the actor's own zone."""
    suffix = uuid.uuid4().hex[:6]
    org_a = Organization(
        kind="leshoz", code=f"mga-{suffix}", name={"uz_cyrl": "А"}, parent_id=agency.id
    )
    org_b = Organization(
        kind="leshoz", code=f"mgb-{suffix}", name={"uz_cyrl": "Б"}, parent_id=agency.id
    )
    db.add_all([org_a, org_b])
    await db.flush()

    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    manager.organization_id = org_a.id
    user_b = await make_user(db, organization_id=org_b.id)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users", params={"organization_id": str(org_b.id)})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["items"]}
    assert str(user_b.id) in ids


async def test_list_filters_and_search(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db, role_code="gis_specialist", status="blocked")
    target.full_name = f"Filt Target {suffix}"
    target.login = f"filt-{suffix}"
    other = await make_user(db, role_code="executor_staff")
    other.full_name = f"Filt Other {suffix}"
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        by_role = await client.get(
            f"{API}/admin/users", params={"role_code": "gis_specialist", "q": suffix}
        )
        by_q = await client.get(f"{API}/admin/users", params={"q": f"filt-{suffix}"})
        by_status = await client.get(
            f"{API}/admin/users", params={"status": "blocked", "q": suffix}
        )
    assert by_role.status_code == 200, by_role.text
    assert {row["id"] for row in by_role.json()["items"]} == {str(target.id)}
    assert {row["id"] for row in by_q.json()["items"]} == {str(target.id)}
    assert {row["id"] for row in by_status.json()["items"]} == {str(target.id)}
    assert other.id  # sanity: the other user exists but never matches above


async def test_get_user_card(db):
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    target = await make_user(db, role_code="gis_specialist")
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{target.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(target.id)
    assert body["role_code"] == "gis_specialist"


async def test_get_unknown_user_is_404(db):
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"


async def test_get_user_card_is_zone_scoped_for_view_only(db, agency):
    """Controller ruling on Task 5 review: ruling 7's zone rule applies to reading
    users generally, not only `GET /admin/users` — a view-only holder must not be
    able to read a card outside their own zone by id."""
    suffix = uuid.uuid4().hex[:6]
    org_a = Organization(
        kind="leshoz", code=f"gva-{suffix}", name={"uz_cyrl": "А"}, parent_id=agency.id
    )
    org_b = Organization(
        kind="leshoz", code=f"gvb-{suffix}", name={"uz_cyrl": "Б"}, parent_id=agency.id
    )
    db.add_all([org_a, org_b])
    await db.flush()

    viewer, token, csrf = await signed_in_with(db, USERS_VIEW)
    viewer.organization_id = org_a.id
    in_zone = await make_user(db, organization_id=org_a.id)
    out_of_zone = await make_user(db, organization_id=org_b.id)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        ok = await client.get(f"{API}/admin/users/{in_zone.id}")
        blocked = await client.get(f"{API}/admin/users/{out_of_zone.id}")
    assert ok.status_code == 200, ok.text
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "ERR-ACL-002"


async def test_get_user_card_manage_holder_sees_out_of_zone(db, agency):
    suffix = uuid.uuid4().hex[:6]
    org_a = Organization(
        kind="leshoz", code=f"gma-{suffix}", name={"uz_cyrl": "А"}, parent_id=agency.id
    )
    org_b = Organization(
        kind="leshoz", code=f"gmb-{suffix}", name={"uz_cyrl": "Б"}, parent_id=agency.id
    )
    db.add_all([org_a, org_b])
    await db.flush()

    manager, token, csrf = await signed_in_with(db, USERS_MANAGE)
    manager.organization_id = org_a.id
    out_of_zone = await make_user(db, organization_id=org_b.id)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{out_of_zone.id}")
    assert r.status_code == 200, r.text


async def test_create_requires_manage_not_just_view(db):
    """Write routes are gated behind USERS_MANAGE alone — a view-only grant, which
    is enough for the list/get routes, must not be enough here."""
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={
                "login": f"blocked-{uuid.uuid4().hex[:8]}",
                "full_name": "X",
                "role_code": "executor_staff",
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_staff_returns_credentials_once(db, agency):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={
                "login": f"newstaff-{suffix}",
                "full_name": "New Staff",
                "role_code": "executor_staff",
                "organization_id": str(agency.id),
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["one_time_password"]
    assert body["totp_uri"].startswith("otpauth://")
    assert body["user"]["role_code"] == "executor_staff"
    assert body["user"]["organization_id"] == str(agency.id)
    validate_password_policy(body["one_time_password"])  # must not raise

    row = (await db.execute(select(User).where(User.login == f"newstaff-{suffix}"))).scalar_one()
    assert row.must_change_password is True
    assert row.mfa_secret is not None
    assert verify_password(body["one_time_password"], row.password_hash)


async def test_create_applicant_role_rejected(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={
                "login": f"appl-{uuid.uuid4().hex[:8]}",
                "full_name": "X",
                "role_code": "applicant",
            },
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "staff_roles_only"


async def test_create_duplicate_pinfl_points_at_existing_user(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    pinfl = f"{int(suffix, 16) % 10**14:014d}"
    existing = await make_user(db, pinfl=pinfl)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={
                "login": f"dup-{suffix}",
                "full_name": "Dup",
                "role_code": "executor_staff",
                "pinfl": pinfl,
            },
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["existing_user_id"] == str(existing.id)


async def test_create_duplicate_login_points_at_existing_user(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    existing = await make_user(db)
    existing.login = f"duplogin-{suffix}"
    await db.flush()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={"login": existing.login, "full_name": "Dup2", "role_code": "executor_staff"},
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["existing_user_id"] == str(existing.id)


async def test_patch_staffifies_an_applicant(db, agency):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    applicant_user = await make_user(db, role_code="applicant")
    applicant_user.login = None  # simulate an auto-created OneID applicant
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        missing_login = await client.patch(
            f"{API}/admin/users/{applicant_user.id}",
            json={"role_code": "executor_staff", "organization_id": str(agency.id)},
        )
        assert missing_login.status_code == 422, missing_login.text
        assert missing_login.json()["error"]["details"]["reason"] == "login_required"

        staffified = await client.patch(
            f"{API}/admin/users/{applicant_user.id}",
            json={
                "role_code": "executor_staff",
                "login": f"newstaff-{suffix}",
                "organization_id": str(agency.id),
            },
        )
    assert staffified.status_code == 200, staffified.text
    assert staffified.json()["role_code"] == "executor_staff"
    assert staffified.json()["login"] == f"newstaff-{suffix}"


async def test_block_revokes_sessions_and_audits(db):
    _, admin_token, admin_csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db)
    _, target_token, _ = await make_session(db, target)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, admin_token, admin_csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/block", json={"reason": "misuse"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "blocked"

    async with make_client(app, lifespan=True) as client2:
        client2.cookies.set("session", target_token)
        me = await client2.get(f"{API}/auth/me")
    assert me.status_code == 401

    entry = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "user.block", AuditLog.object_id == target.id)
        )
    ).scalar_one()
    assert entry.new_value["reason"] == "misuse"


async def test_unblock_restores_access(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(
        db,
        status="blocked",
        failed_login_count=3,
        locked_until=datetime.now(UTC) + timedelta(hours=1),
    )
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/unblock")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    await db.refresh(target)
    assert target.failed_login_count == 0
    assert target.locked_until is None


async def test_cannot_block_or_delete_self(db):
    admin, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        block = await client.post(f"{API}/admin/users/{admin.id}/block", json={"reason": "x"})
        delete = await client.post(f"{API}/admin/users/{admin.id}/delete")
    assert block.status_code == 422, block.text
    assert block.json()["error"]["details"]["reason"] == "own_account"
    assert delete.status_code == 422, delete.text
    assert delete.json()["error"]["details"]["reason"] == "own_account"


async def test_delete_soft(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/delete")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"

    await db.refresh(target)
    assert target.status == "deleted"


async def test_reset_password_returns_new_one_and_revokes_sessions(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    old_password = "Old-Password-1!"
    target = await make_user(db, password_hash=hash_password(old_password))
    _, target_token, _ = await make_session(db, target)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/reset-password")
    assert r.status_code == 200, r.text
    new_password = r.json()["one_time_password"]
    assert new_password

    await db.refresh(target)
    assert target.password_hash is not None
    assert not verify_password(old_password, target.password_hash)
    assert verify_password(new_password, target.password_hash)
    assert target.must_change_password is True

    async with make_client(app, lifespan=True) as client2:
        client2.cookies.set("session", target_token)
        me = await client2.get(f"{API}/auth/me")
    assert me.status_code == 401


async def test_reset_mfa_returns_new_uri(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db, mfa_secret=encrypt_str("OLDSECRETOLDSECRET"))
    _, target_token, _ = await make_session(db, target)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/users/{target.id}/reset-mfa")
    assert r.status_code == 200, r.text
    assert r.json()["totp_uri"].startswith("otpauth://")

    await db.refresh(target)
    assert target.mfa_secret is not None
    assert decrypt_str(target.mfa_secret) != "OLDSECRETOLDSECRET"

    async with make_client(app, lifespan=True) as client2:
        client2.cookies.set("session", target_token)
        me = await client2.get(f"{API}/auth/me")
    assert me.status_code == 401


async def test_audit_written_for_create_and_patch(db):
    suffix = uuid.uuid4().hex[:8]
    admin, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        created = await client.post(
            f"{API}/admin/users",
            json={
                "login": f"aud-{suffix}",
                "full_name": "Audit Target",
                "role_code": "executor_staff",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["user"]["id"]
        patched = await client.patch(
            f"{API}/admin/users/{user_id}", json={"full_name": "Audit Target Renamed"}
        )
        assert patched.status_code == 200, patched.text

    forbidden_keys = {"password", "password_hash", "one_time_password", "mfa_secret", "secret"}

    create_entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "user.create", AuditLog.object_id == uuid.UUID(user_id)
            )
        )
    ).scalar_one()
    assert create_entry.user_id == admin.id
    assert not forbidden_keys & set(create_entry.new_value or {})

    patch_entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "user.update", AuditLog.object_id == uuid.UUID(user_id)
            )
        )
    ).scalar_one()
    assert patch_entry.old_value["full_name"] == "Audit Target"
    assert patch_entry.new_value["full_name"] == "Audit Target Renamed"
    assert not forbidden_keys & set(patch_entry.new_value or {})
