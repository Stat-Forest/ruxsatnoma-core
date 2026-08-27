"""Auth service: sessions, login+MFA, passwords. The only door for other modules."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import hash_token, new_token
from app.modules.audit import service as audit
from app.modules.auth import repo
from app.modules.auth.models import Session, User


async def issue_session(
    db: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
) -> tuple[Session, str, str]:
    """Create a session row; returns (row, raw token for the cookie, csrf token)."""
    settings = get_settings()
    token, csrf = new_token(), new_token()
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=csrf,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_absolute_hours),
        ip=ip,
        user_agent=user_agent,
    )
    await repo.add(db, row)
    await audit.log(
        db,
        action="session.create",
        user_id=user.id,
        object_type="session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
    )
    return row, token, csrf


async def revoke_session(db: AsyncSession, session_row: Session, *, reason: str) -> None:
    session_row.revoked_at = datetime.now(UTC)
    await audit.log(
        db,
        action="session.revoke",
        user_id=session_row.user_id,
        object_type="session",
        object_id=session_row.id,
        basis=reason,
    )
