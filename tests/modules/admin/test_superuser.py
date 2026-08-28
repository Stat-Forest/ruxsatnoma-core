"""sys_admin passes permission checks without holding codes (stage 3.3a ruling 2)."""

import uuid

from sqlalchemy import select

from app.main import create_app
from app.modules.audit.models import AuditLog
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"


async def test_sys_admin_needs_no_grants(db, agency):
    suffix = uuid.uuid4().hex[:6]
    user = await make_user(db, role_code="sys_admin")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "territorial",
                "parent_id": str(agency.id),
                "code": f"su-{suffix}",
                "name": {"uz_cyrl": "Ҳудудий бошқарма"},
            },
        )
    assert r.status_code == 201, r.text


async def test_sys_admin_actions_are_still_audited(db, agency):
    suffix = uuid.uuid4().hex[:6]
    user = await make_user(db, role_code="sys_admin")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "territorial",
                "parent_id": str(agency.id),
                "code": f"su-a-{suffix}",
                "name": {"uz_cyrl": "Х"},
            },
        )
    entry = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.user_id == user.id, AuditLog.action == "organization.create")
            .order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert entry.new_value["code"] == f"su-a-{suffix}"


async def test_other_roles_still_need_the_code(db, agency):
    user = await make_user(db, role_code="accountant")
    _, token, csrf = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        auth_client(client, token, csrf)
        r = await client.post(
            f"{API}/admin/organizations",
            json={
                "kind": "territorial",
                "parent_id": str(agency.id),
                "code": "denied-1",
                "name": {"uz_cyrl": "Х"},
            },
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ERR-ACL-001"
