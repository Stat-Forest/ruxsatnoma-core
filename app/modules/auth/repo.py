"""Auth repository: DB access for users, sessions, permissions, otp codes."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import OtpCode, Role, RolePermission, Session, User, UserPermission


async def get_user_by_login(db: AsyncSession, login: str) -> User | None:
    return (await db.execute(select(User).where(User.login == login))).scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def get_role(db: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await db.get(Role, role_id)


async def get_session_by_token_hash(db: AsyncSession, token_hash: str) -> Session | None:
    return (
        await db.execute(select(Session).where(Session.token_hash == token_hash))
    ).scalar_one_or_none()


async def add(db: AsyncSession, obj) -> None:
    db.add(obj)
    await db.flush()


async def permission_codes(db: AsyncSession, user: User) -> set[str]:
    role_codes = (
        await db.execute(
            select(RolePermission.permission_code).where(RolePermission.role_id == user.role_id)
        )
    ).scalars()
    user_codes = (
        await db.execute(
            select(UserPermission.permission_code).where(UserPermission.user_id == user.id)
        )
    ).scalars()
    return set(role_codes) | set(user_codes)


async def get_valid_otp(db: AsyncSession, code_hash: str, purpose: str) -> OtpCode | None:
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(OtpCode).where(
                OtpCode.code_hash == code_hash,
                OtpCode.purpose == purpose,
                OtpCode.used_at.is_(None),
                OtpCode.expires_at > now,
            )
        )
    ).scalar_one_or_none()


async def revoke_other_sessions(db: AsyncSession, user_id: uuid.UUID, *, keep: uuid.UUID) -> None:
    rows = (
        await db.execute(
            select(Session).where(
                Session.user_id == user_id, Session.revoked_at.is_(None), Session.id != keep
            )
        )
    ).scalars()
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
