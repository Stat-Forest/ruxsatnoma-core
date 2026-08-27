"""/auth/me carries permissions; require_permission gates a route; must-change blocks non-exempt."""

import pytest

from app.main import create_app
from app.modules.auth import permissions
from app.modules.auth.models import RolePermission, UserPermission
from tests.conftest import make_client
from tests.modules.auth.test_sessions import make_session, make_user

API = "/api/v1"

# Registered once per process at import time: module-level PERMISSIONS persists
# across tests within one pytest run, and require_permission() now rejects an
# unregistered code (F10) — re-registering here on a later import would itself
# raise ValueError, so this must run exactly once, at module import.
permissions.register(
    {"test.secret": "throwaway (test_me_rbac)", "test.secret2": "throwaway (test_me_rbac)"}
)


async def test_me_lists_role_and_user_permissions(db):
    user = await make_user(db)
    db.add(RolePermission(role_id=user.role_id, permission_code="test.role_perm"))
    db.add(UserPermission(user_id=user.id, permission_code="test.user_perm"))
    await db.flush()
    _, token, _ = await make_session(db, user)
    await db.commit()
    app = create_app()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/auth/me")
    assert r.status_code == 200
    perms = r.json()["permissions"]
    assert "test.role_perm" in perms and "test.user_perm" in perms
    # cleanup of committed grants happens via fixture rollback? No — they were flushed
    # in this session and committed together with the session row; delete them:
    from sqlalchemy import delete

    await db.execute(
        delete(RolePermission).where(RolePermission.permission_code == "test.role_perm")
    )
    await db.execute(
        delete(UserPermission).where(UserPermission.permission_code == "test.user_perm")
    )
    await db.commit()


async def test_require_permission_403_without_grant(db, engine):
    # a throwaway app route protected by require_permission, mounted only in this test
    from typing import Annotated

    from fastapi import Depends

    from app.modules.auth.deps import require_permission
    from app.modules.auth.models import User

    app = create_app()

    @app.get(f"{API}/test-protected")
    async def protected(
        user: Annotated[User, Depends(require_permission("test.secret"))],
    ) -> dict:
        return {"ok": True}

    user = await make_user(db)
    _, token, _ = await make_session(db, user)
    await db.commit()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/test-protected")
    assert r.status_code == 403 and r.json()["error"]["code"] == "ERR-ACL-001"

    from sqlalchemy import func, select

    from app.db import make_session_factory
    from app.modules.audit.models import AuditLog

    async with make_session_factory(engine)() as fresh:
        denied = (
            await fresh.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.user_id == user.id,
                    AuditLog.action == "access.denied",
                    AuditLog.basis == "test.secret",
                )
            )
        ).scalar()
        assert denied == 1


async def test_require_permission_ok_with_grant(db):
    from typing import Annotated

    from fastapi import Depends

    from app.modules.auth.deps import require_permission
    from app.modules.auth.models import User

    app = create_app()

    @app.get(f"{API}/test-protected2")
    async def protected(
        user: Annotated[User, Depends(require_permission("test.secret2"))],
    ) -> dict:
        return {"ok": True}

    user = await make_user(db)
    db.add(UserPermission(user_id=user.id, permission_code="test.secret2"))
    _, token, _ = await make_session(db, user)
    await db.commit()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/test-protected2")
    assert r.status_code == 200
    from sqlalchemy import delete

    await db.execute(delete(UserPermission).where(UserPermission.permission_code == "test.secret2"))
    await db.commit()


async def test_must_change_password_blocks_non_exempt_route(db):
    from typing import Annotated

    from fastapi import Depends

    from app.modules.auth.deps import get_current_user
    from app.modules.auth.models import User

    app = create_app()

    @app.get(f"{API}/test-normal")
    async def normal(user: Annotated[User, Depends(get_current_user)]) -> dict:
        return {"ok": True}

    user = await make_user(db, must_change_password=True)
    _, token, _ = await make_session(db, user)
    await db.commit()
    async with make_client(app, lifespan=True) as client:
        client.cookies.set("session", token)
        r = await client.get(f"{API}/test-normal")
    assert r.status_code == 403 and r.json()["error"]["code"] == "ERR-AUTH-007"


def test_register_duplicate_code_raises():
    with pytest.raises(ValueError):
        permissions.register({"test.secret": "duplicate registration"})
