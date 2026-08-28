"""Auth repository: DB access for users, sessions, permissions, otp codes."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.modules.auth.models import (
    Applicant,
    OtpCode,
    Representation,
    Role,
    RolePermission,
    Session,
    User,
    UserPermission,
)


async def get_user_by_login(db: AsyncSession, login: str) -> User | None:
    return (await db.execute(select(User).where(User.login == login))).scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def get_role(db: AsyncSession, role_id: uuid.UUID) -> Role | None:
    return await db.get(Role, role_id)


async def get_user_by_pinfl(db: AsyncSession, pinfl: str) -> User | None:
    return (await db.execute(select(User).where(User.pinfl == pinfl))).scalar_one_or_none()


async def get_role_by_code(db: AsyncSession, code: str) -> Role | None:
    return (await db.execute(select(Role).where(Role.code == code))).scalar_one_or_none()


async def get_session_by_token_hash(db: AsyncSession, token_hash: str) -> Session | None:
    return (
        await db.execute(select(Session).where(Session.token_hash == token_hash))
    ).scalar_one_or_none()


async def add(db: AsyncSession, obj: Base) -> None:
    db.add(obj)
    await db.flush()


async def increment_failed_logins(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Atomic SQL increment (returning the new value) instead of a Python
    read-modify-write, which would lose updates under concurrent attempts."""
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(failed_login_count=User.failed_login_count + 1)
        .returning(User.failed_login_count)
    )
    return result.scalar_one()


async def lock_user(db: AsyncSession, user_id: uuid.UUID, *, until: datetime) -> None:
    """Sets locked_until and resets the failed-attempt counter in one statement."""
    await db.execute(
        update(User).where(User.id == user_id).values(locked_until=until, failed_login_count=0)
    )


async def role_code(db: AsyncSession, user: User) -> str | None:
    """The user's role code, or None if the role row vanished (should not happen: FK)."""
    stmt = select(Role.code).where(Role.id == user.role_id)
    return (await db.execute(stmt)).scalar_one_or_none()


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
    """Looks up by code_hash alone — sufficient for 256-bit tokens; NEVER reuse for
    short numeric codes (phone/email OTP in 3.2b) without adding a target/user filter.
    """
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


async def count_recent_otps(db: AsyncSession, *, target: str, purpose: str, since: datetime) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(OtpCode)
        .where(OtpCode.target == target, OtpCode.purpose == purpose, OtpCode.created_at >= since)
    )
    return result.scalar_one()


async def latest_pending_otp(db: AsyncSession, *, target: str, purpose: str) -> OtpCode | None:
    now = datetime.now(UTC)
    return (
        await db.execute(
            select(OtpCode)
            .where(
                OtpCode.target == target,
                OtpCode.purpose == purpose,
                OtpCode.used_at.is_(None),
                OtpCode.expires_at > now,
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def other_active_sessions(
    db: AsyncSession, user_id: uuid.UUID, *, exclude: uuid.UUID
) -> list[Session]:
    """Non-revoked sessions of `user_id` other than `exclude` (pure SELECT — the
    caller decides how to revoke them, e.g. via service.revoke_session for the audit
    trail each revocation needs)."""
    rows = (
        await db.execute(
            select(Session).where(
                Session.user_id == user_id, Session.revoked_at.is_(None), Session.id != exclude
            )
        )
    ).scalars()
    return list(rows)


async def get_own_applicant(db: AsyncSession, user_id: uuid.UUID) -> Applicant | None:
    return (
        await db.execute(select(Applicant).where(Applicant.owner_user_id == user_id))
    ).scalar_one_or_none()


async def effective_representations(
    db: AsyncSession, user_id: uuid.UUID, today: date
) -> list[tuple[Representation, Applicant]]:
    rows = await db.execute(
        select(Representation, Applicant)
        .join(Applicant, Representation.applicant_id == Applicant.id)
        .where(
            Representation.user_id == user_id,
            Representation.status == "active",
            (Representation.valid_until.is_(None)) | (Representation.valid_until >= today),
        )
        .order_by(Representation.created_at)
    )
    return [(rep, app) for rep, app in rows.all()]


async def get_applicant_by_stir(db: AsyncSession, stir: str) -> Applicant | None:
    return (await db.execute(select(Applicant).where(Applicant.stir == stir))).scalar_one_or_none()


async def get_effective_representation(
    db: AsyncSession, *, applicant_id: uuid.UUID, user_id: uuid.UUID, today: date
) -> Representation | None:
    return (
        await db.execute(
            select(Representation).where(
                Representation.applicant_id == applicant_id,
                Representation.user_id == user_id,
                Representation.status == "active",
                (Representation.valid_until.is_(None)) | (Representation.valid_until >= today),
            )
        )
    ).scalar_one_or_none()
