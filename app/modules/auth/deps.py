"""Request dependencies: current session/user. Other modules import from here."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import settings_store
from app.core.deps import get_db
from app.core.errors import err
from app.core.security import hash_token
from app.core.time import business_today
from app.modules.audit import service as audit
from app.modules.auth import repo, service
from app.modules.auth.models import Session, User
from app.modules.auth.permissions import PERMISSIONS

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# (method, path) pairs a must-change-password user may still call (ruling 8).
# Method-aware so a route that shares a path with an exempt one but a different
# verb (e.g. PATCH /auth/me alongside GET /auth/me) is NOT exempted for free.
_MUST_CHANGE_ALLOWED = {
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/password/change"),
}
# (method, path) pairs an applicant may call before completing C2 registration
# (ruling 9); /api/v1/refs/* is allowed as a prefix regardless of method — the
# registration form needs catalogs and refs is read-only (GET) in practice.
_REGISTRATION_EXEMPT = {
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/complete-registration"),
}
# Role that passes every permission gate (stage 3.3a ruling 2)
SUPERUSER_ROLE = "sys_admin"


def _origin_allowed(request: Request, origin: str) -> bool:
    """Same-origin requests and configured adminka origins only (ruling 3).
    Requests without an Origin header (curl, server-to-server) are left to the CSRF
    token, which they cannot guess."""
    if origin in get_settings().cors_origins:
        return True
    return origin == str(request.base_url).rstrip("/")


async def get_current_session(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> Session:
    token = request.cookies.get("session")
    if not token:
        raise err("ERR-AUTH-002")
    row = await repo.get_session_by_token_hash(db, hash_token(token))
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise err("ERR-AUTH-002")
    idle_limit = timedelta(minutes=await settings_store.get_int(db, "session_idle_minutes"))
    if row.last_seen_at + idle_limit <= now:
        await service.revoke_session(db, row, reason="idle timeout")
        await db.commit()  # the revocation must survive the 401 below (ruling 2 pattern)
        raise err("ERR-AUTH-002")
    if request.method in _MUTATING:
        origin = request.headers.get("Origin")
        if origin is not None and not _origin_allowed(request, origin):
            await audit.log(
                db, action="access.denied", user_id=row.user_id, result="denied", basis="origin"
            )
            await db.commit()
            raise err("ERR-AUTH-006")
        header = request.headers.get("X-CSRF-Token")
        if not header or not secrets.compare_digest(header, row.csrf_token):
            await audit.log(
                db, action="access.denied", user_id=row.user_id, result="denied", basis="csrf"
            )
            await db.commit()
            raise err("ERR-AUTH-006")
    # last_seen_at only needs minute precision for the idle timeout above, so skip
    # the write (and the hot-row contention it causes) on most requests.
    if now - row.last_seen_at > timedelta(seconds=60):
        row.last_seen_at = now
    return row


async def get_current_user(
    request: Request,
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await repo.get_user(db, session_row.user_id)
    if user is None or user.status != "active":
        raise err("ERR-AUTH-002")
    # business_today(), not a UTC date.today()/now.date() (same Asia/Tashkent rule as
    # admin.repo/admin.service, finding 11): valid_until is a calendar day, and a
    # server-local/UTC day would keep an expired fixed-term account (e.g. the
    # prosecutor's) authenticating for up to 5 hours after midnight Tashkent time.
    if user.valid_until is not None and user.valid_until < business_today():
        raise err("ERR-AUTH-002")
    if user.must_change_password and (request.method, request.url.path) not in _MUST_CHANGE_ALLOWED:
        raise err("ERR-AUTH-007")
    if await repo.role_code(db, user) == "applicant":
        path = request.url.path
        exempt = (request.method, path) in _REGISTRATION_EXEMPT or path.startswith("/api/v1/refs/")
        if not exempt:
            if await repo.get_own_applicant(db, user.id) is None:
                raise err("ERR-AUTH-008")
    return user


async def _authorize(db: AsyncSession, user: User, wanted: set[str], *, basis: str) -> None:
    """Shared body of `require_permission`/`require_any_permission`.

    Stage 3.3a ruling 2: sys_admin passes every gate — the action itself is still
    audited by the service that performs it, and the DB-level append-only triggers
    on audit_log are unaffected by this bypass. Otherwise `user` must hold at least
    one of `wanted` (role ∪ personal grants); on denial, the audit entry is written
    and committed BEFORE raising (early-commit pattern) — the exception's own
    rollback in `get_db` would otherwise discard the very entry meant to record it.
    """
    if await repo.role_code(db, user) == SUPERUSER_ROLE:
        return
    held = await repo.permission_codes(db, user)
    if held.isdisjoint(wanted):
        await audit.log(db, action="access.denied", user_id=user.id, result="denied", basis=basis)
        await db.commit()
        raise err("ERR-ACL-001", details={"permission": basis})


def require_permission(code: str):
    """Dependency factory: current user must hold `code` (role ∪ personal grants).

    `code` must already be registered (own module's `permissions.register` call at
    import time) — checked here, at factory-call time, so a typo'd permission code
    fails at startup/route-definition instead of silently 403-ing every request.
    """
    if code not in PERMISSIONS:
        raise ValueError(f"permission code not registered: {code!r}")

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        await _authorize(db, user, {code}, basis=code)
        return user

    return _check


def require_any_permission(*codes: str):
    """Dependency factory: current user must hold at least ONE of `codes` (role ∪
    personal grants) — for read routes that a view-level AND a manage-level grant
    should both satisfy (e.g. `GET /admin/users`: `auth.users.view` OR
    `auth.users.manage`). Same registration check and sys_admin bypass as
    `require_permission`, whose internals it shares via `_authorize`.
    """
    if not codes:
        raise ValueError("require_any_permission needs at least one code")
    for code in codes:
        if code not in PERMISSIONS:
            raise ValueError(f"permission code not registered: {code!r}")

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        await _authorize(db, user, set(codes), basis="|".join(codes))
        return user

    return _check
