"""/admin/roles, /admin/permissions, /admin/users/{id}/permissions (С23): role CRUD,
permission-registry coverage, and personal grants."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from app.modules.auth.models import Role, RolePermission
from app.modules.auth.permissions import SESSIONS_REVOKE_ANY, USERS_MANAGE, USERS_VIEW
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with
from tests.modules.auth.test_sessions import make_user

API = "/api/v1"


async def test_list_roles_requires_permission(db):
    _, token, csrf = await signed_in_with(db)  # no grants
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/roles")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_create_role_requires_manage_not_view(db):
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/roles",
            json={"code": f"role-{uuid.uuid4().hex[:8]}", "name": {"uz_cyrl": "Х", "uz_latn": "X"}},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"


async def test_list_roles_shows_seeded_roles_and_permission_codes(db):
    """Migration 0003 seeds 11 is_system roles; migration 0007 grants auth.users.view
    to central_admin/leadership/executor_head — the list must reflect both via the
    roles_with_stats LEFT JOIN + array_agg."""
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/roles")
    assert r.status_code == 200, r.text
    by_code = {row["code"]: row for row in r.json()}
    assert by_code["sys_admin"]["is_system"] is True
    assert by_code["sys_admin"]["status"] == "active"
    for code in ("central_admin", "leadership", "executor_head"):
        assert "auth.users.view" in by_code[code]["permission_codes"]


async def test_list_roles_holders_count_excludes_deleted(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"holders-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    await make_user(db, role_code=role.code)  # active holder
    deleted_holder = await make_user(db, role_code=role.code)
    deleted_holder.status = "deleted"
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/roles")
    assert r.status_code == 200, r.text
    entry = next(row for row in r.json() if row["code"] == role.code)
    assert entry["holders"] == 1


async def test_create_role(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/roles",
            json={
                "code": f"newrole-{suffix}",
                "name": {"uz_cyrl": "Янги роль", "uz_latn": "Yangi rol"},
                "description": {"uz_cyrl": "Тавсиф", "uz_latn": "Tavsif"},
                "max_approve_amount": "500000.00",
                "max_approve_area": "12.5",
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == f"newrole-{suffix}"
    assert body["is_system"] is False
    assert body["status"] == "active"
    assert body["holders"] == 0
    assert body["permission_codes"] == []
    assert Decimal(str(body["max_approve_amount"])) == Decimal("500000.00")
    assert Decimal(str(body["max_approve_area"])) == Decimal("12.5")

    entry = (
        await db.execute(
            select(AuditLog).where(
                AuditLog.action == "role.create", AuditLog.object_id == uuid.UUID(body["id"])
            )
        )
    ).scalar_one()
    assert entry.new_value["code"] == f"newrole-{suffix}"


async def test_create_role_duplicate_code(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"dup-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/roles",
            json={"code": f"dup-{suffix}", "name": {"uz_cyrl": "Й", "uz_latn": "Y"}},
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "duplicate_code"


async def test_create_role_copy_from_copies_codes_and_limits(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    source = Role(
        code=f"src-{suffix}",
        name={"uz_cyrl": "Асл", "uz_latn": "Asl"},
        max_approve_amount=Decimal("250000.00"),
        max_approve_area=Decimal("7.25"),
    )
    db.add(source)
    await db.flush()
    db.add(RolePermission(role_id=source.id, permission_code=USERS_VIEW))
    db.add(RolePermission(role_id=source.id, permission_code=USERS_MANAGE))
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/roles",
            json={
                "code": f"copy-{suffix}",
                "name": {"uz_cyrl": "Нусха", "uz_latn": "Nusxa"},
                "copy_from": source.code,
            },
        )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body["permission_codes"]) == {USERS_VIEW, USERS_MANAGE}
    assert Decimal(str(body["max_approve_amount"])) == Decimal("250000.00")
    assert Decimal(str(body["max_approve_area"])) == Decimal("7.25")


async def test_create_role_copy_from_unknown_code_is_404(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/roles",
            json={
                "code": f"badcopy-{uuid.uuid4().hex[:8]}",
                "name": {"uz_cyrl": "Х", "uz_latn": "X"},
                "copy_from": "does-not-exist",
            },
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
    assert r.json()["error"]["details"] == {"role_code": "does-not-exist"}


async def test_patch_role_renames(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"patch-{suffix}", name={"uz_cyrl": "Эски", "uz_latn": "Eski"})
    db.add(role)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/roles/{role.id}",
            json={"name": {"uz_cyrl": "Янги", "uz_latn": "Yangi"}, "max_approve_amount": "999.99"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"]["uz_cyrl"] == "Янги"
    assert Decimal(str(body["max_approve_amount"])) == Decimal("999.99")

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "role.update", AuditLog.object_id == role.id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value["name"]["uz_cyrl"] == "Эски"
    assert entry.new_value["name"]["uz_cyrl"] == "Янги"


async def test_set_role_permissions_rejects_unknown_code(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"setperm-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(
            f"{API}/admin/roles/{role.id}/permissions", json={"codes": ["no.such.permission"]}
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "unknown_permission"


async def test_set_role_permissions_rejects_applicant_role(db):
    """R5b (final review): staff permission codes must never be grantable to the
    public applicant role — `applicant` never goes through `_staff_role_or_422`
    (it is created via OneID/E-IMZO, not this API), so this is the only gate."""
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    applicant_role_id = (
        await db.execute(select(Role.id).where(Role.code == "applicant"))
    ).scalar_one()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(
            f"{API}/admin/roles/{applicant_role_id}/permissions", json={"codes": [USERS_VIEW]}
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "applicant_role_restricted"


async def test_set_role_permissions_audits_old_and_new(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"setperm2-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    db.add(RolePermission(role_id=role.id, permission_code=USERS_VIEW))
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(
            f"{API}/admin/roles/{role.id}/permissions", json={"codes": [USERS_MANAGE]}
        )
    assert r.status_code == 200, r.text
    assert r.json()["permission_codes"] == [USERS_MANAGE]

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "role.permissions_set", AuditLog.object_id == role.id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value["codes"] == [USERS_VIEW]
    assert entry.new_value["codes"] == [USERS_MANAGE]


async def test_archive_role_blocked_by_active_holder(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"inuse-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    await make_user(db, role_code=role.code)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/roles/{role.id}/archive")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "role_in_use"


async def test_archive_role_not_blocked_by_deleted_holder(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"delheld-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"})
    db.add(role)
    await db.flush()
    holder = await make_user(db, role_code=role.code)
    holder.status = "deleted"
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/roles/{role.id}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"


async def test_archive_system_role_rejected(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = (await db.execute(select(Role).where(Role.code == "prosecutor"))).scalar_one()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(f"{API}/admin/roles/{role.id}/archive")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "system_role"


async def test_archived_role_rejected_by_create_user(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"arch-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"}, status="archived")
    db.add(role)
    await db.flush()
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/users",
            json={"login": f"archrole-{suffix}", "full_name": "X", "role_code": role.code},
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "role_archived"


async def test_archived_role_rejected_by_patch_user(db):
    suffix = uuid.uuid4().hex[:8]
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    role = Role(code=f"arch2-{suffix}", name={"uz_cyrl": "Х", "uz_latn": "X"}, status="archived")
    db.add(role)
    await db.flush()
    target = await make_user(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(f"{API}/admin/users/{target.id}", json={"role_code": role.code})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "role_archived"


async def test_list_permissions_marks_unassigned_codes(db):
    """auth.sessions.revoke_any is never granted to any role anywhere in this suite
    (unlike auth.users.view, seeded to 3 roles by migration 0007) — it must come
    back with roles == []; auth.users.view must NOT be assumed empty."""
    _, token, csrf = await signed_in_with(db, USERS_VIEW)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/permissions")
    assert r.status_code == 200, r.text
    by_code = {row["code"]: row for row in r.json()}
    assert by_code[SESSIONS_REVOKE_ANY]["roles"] == []
    assert {"central_admin", "leadership", "executor_head"} <= set(by_code[USERS_VIEW]["roles"])


async def test_user_permissions_get_and_put_replaces_set_with_audit(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        empty = await client.get(f"{API}/admin/users/{target.id}/permissions")
        assert empty.status_code == 200, empty.text
        assert empty.json()["codes"] == []

        first = await client.put(
            f"{API}/admin/users/{target.id}/permissions", json={"codes": [USERS_VIEW]}
        )
        assert first.status_code == 200, first.text
        assert first.json()["codes"] == [USERS_VIEW]

        replaced = await client.put(
            f"{API}/admin/users/{target.id}/permissions", json={"codes": [USERS_MANAGE]}
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["codes"] == [USERS_MANAGE]

        confirm = await client.get(f"{API}/admin/users/{target.id}/permissions")
        assert confirm.json()["codes"] == [USERS_MANAGE]

    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "user.permissions_set", AuditLog.object_id == target.id)
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.old_value["codes"] == [USERS_VIEW]
    assert entry.new_value["codes"] == [USERS_MANAGE]


async def test_user_permissions_put_unknown_code_rejected(db):
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    target = await make_user(db)
    await db.commit()

    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.put(
            f"{API}/admin/users/{target.id}/permissions", json={"codes": ["nope.nope"]}
        )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "unknown_permission"


async def test_user_not_found_permissions_is_404(db):
    """`details` must be non-empty (populated by `_user_or_404`) — a bare route-miss
    404 (Starlette's generic handler) carries `details: {}`, so this also confirms
    the route itself exists and the lookup actually ran."""
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    missing_id = uuid.uuid4()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.get(f"{API}/admin/users/{missing_id}/permissions")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
    assert r.json()["error"]["details"] == {"user": str(missing_id)}


async def test_role_not_found_is_404(db):
    """Same discriminating-detail reasoning as test_user_not_found_permissions_is_404."""
    _, token, csrf = await signed_in_with(db, USERS_MANAGE)
    missing_id = uuid.uuid4()
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.patch(
            f"{API}/admin/roles/{missing_id}", json={"name": {"uz_cyrl": "Х", "uz_latn": "X"}}
        )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ERR-SYS-003"
    assert r.json()["error"]["details"] == {"role": str(missing_id)}
