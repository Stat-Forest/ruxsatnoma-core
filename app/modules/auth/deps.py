"""Request dependencies: current session/user. Other modules import from here."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import get_db
from app.core.errors import err
from app.core.security import hash_token
from app.modules.auth import repo, service
from app.modules.auth.models import Session, User

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Paths a must-change-password user may still call (ruling 8)
_MUST_CHANGE_ALLOWED = {"/api/v1/auth/me", "/api/v1/auth/logout", "/api/v1/auth/password/change"}


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
    idle_limit = timedelta(minutes=get_settings().session_idle_minutes)
    if row.last_seen_at + idle_limit <= now:
        await service.revoke_session(db, row, reason="idle timeout")
        await db.commit()  # the revocation must survive the 401 below (ruling 2 pattern)
        raise err("ERR-AUTH-002")
    if request.method in _MUTATING:
        header = request.headers.get("X-CSRF-Token")
        if not header or header != row.csrf_token:
            raise err("ERR-AUTH-006")
    row.last_seen_at = now
    return row


async def get_current_user(
    request: Request,
    session_row: Annotated[Session, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    user = await repo.get_user(db, session_row.user_id)
    now = datetime.now(UTC)
    if user is None or user.status != "active":
        raise err("ERR-AUTH-002")
    if user.valid_until is not None and user.valid_until < now.date():
        raise err("ERR-AUTH-002")
    if user.must_change_password and request.url.path not in _MUST_CHANGE_ALLOWED:
        raise err("ERR-AUTH-007")
    return user


def require_permission(code: str):
    """Dependency factory: current user must hold `code` (role ∪ personal grants)."""

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> User:
        if code not in await repo.permission_codes(db, user):
            raise err("ERR-ACL-001", details={"permission": code})
        return user

    return _check
