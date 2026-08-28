"""Auth repository: DB access for users, sessions, permissions, otp codes."""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, delete, func, or_, select, update
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


async def role_permission_codes(db: AsyncSession, role_id: uuid.UUID) -> list[str]:
    result = await db.execute(
        select(RolePermission.permission_code)
        .where(RolePermission.role_id == role_id)
        .order_by(RolePermission.permission_code)
    )
    return list(result.scalars())


async def user_permission_codes(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Personal grants only (`user_permissions`) — unlike `permission_codes` above,
    which merges in the role's grants too, the С23 personal-grants admin API
    (GET/PUT /admin/users/{id}/permissions) needs the un-merged set."""
    result = await db.execute(
        select(UserPermission.permission_code).where(UserPermission.user_id == user_id)
    )
    return set(result.scalars())


async def role_codes_by_permission(db: AsyncSession) -> dict[str, list[str]]:
    """Which role codes grant each permission code (`GET /admin/permissions`
    coverage view, С23) — a code held by no role is simply absent from the dict;
    callers default to `[]`."""
    rows = (
        await db.execute(
            select(RolePermission.permission_code, Role.code)
            .join(Role, Role.id == RolePermission.role_id)
            .order_by(RolePermission.permission_code, Role.code)
        )
    ).all()
    result: dict[str, list[str]] = {}
    for permission_code, role_code in rows:
        result.setdefault(permission_code, []).append(role_code)
    return result


async def count_role_holders(db: AsyncSession, role_id: uuid.UUID) -> int:
    """Non-deleted users currently assigned `role_id` — same "non-deleted" rule
    `roles_with_stats` counts by, reused as the archive-guard check for one role."""
    result = await db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role_id == role_id, User.status != "deleted")
    )
    return result.scalar_one()


async def roles_with_stats(db: AsyncSession) -> list[tuple[Role, int, list[str]]]:
    """Every role with its holder count (non-deleted users) and permission codes, in
    one query (LEFT JOIN + array_agg) — avoids an N+1 across the admin roles list
    (С23)."""
    holders = (
        select(User.role_id, func.count().label("holders"))
        .where(User.status != "deleted")
        .group_by(User.role_id)
        .subquery()
    )
    perms = (
        select(
            RolePermission.role_id,
            func.array_agg(RolePermission.permission_code).label("codes"),
        )
        .group_by(RolePermission.role_id)
        .subquery()
    )
    stmt = (
        select(Role, func.coalesce(holders.c.holders, 0), perms.c.codes)
        .outerjoin(holders, holders.c.role_id == Role.id)
        .outerjoin(perms, perms.c.role_id == Role.id)
        .order_by(Role.code)
    )
    rows = (await db.execute(stmt)).all()
    return [
        (role, holder_count, sorted(codes) if codes else []) for role, holder_count, codes in rows
    ]


async def set_role_permissions(db: AsyncSession, role_id: uuid.UUID, codes: Iterable[str]) -> None:
    """Replace-set: DELETE then INSERT in the caller's transaction — role_permissions
    has no updated_at to key a diff-patch on, same reasoning as `set_user_permissions`."""
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    db.add_all([RolePermission(role_id=role_id, permission_code=code) for code in codes])
    await db.flush()


async def set_user_permissions(
    db: AsyncSession, user_id: uuid.UUID, codes: Iterable[str], *, granted_by: uuid.UUID
) -> None:
    """Replace-set for personal grants (С23) — same DELETE+INSERT reasoning as
    `set_role_permissions`."""
    await db.execute(delete(UserPermission).where(UserPermission.user_id == user_id))
    db.add_all(
        [
            UserPermission(user_id=user_id, permission_code=code, granted_by=granted_by)
            for code in codes
        ]
    )
    await db.flush()


async def list_users(
    db: AsyncSession,
    *,
    role_code: str | None,
    status: str | None,
    organization_id: uuid.UUID | None,
    region_id: uuid.UUID | None,
    q: str | None,
    zone: ColumnElement[bool] | None,
    offset: int,
    limit: int,
) -> tuple[list[User], int]:
    """Admin user listing (С23). Filters combine with AND; `q` is an ILIKE search
    across login/full_name/pinfl. `zone` is an optional extra WHERE clause the
    caller builds via `core.abac.zone_filter` — this repo stays agnostic of ABAC,
    it just ANDs in whatever boolean expression it is handed (or none at all, for
    a manage-holder who is not zone-restricted)."""
    stmt = select(User)
    if role_code is not None:
        stmt = stmt.join(Role, Role.id == User.role_id).where(Role.code == role_code)
    if status is not None:
        stmt = stmt.where(User.status == status)
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    if region_id is not None:
        stmt = stmt.where(User.region_id == region_id)
    if q is not None:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                User.login.ilike(pattern),
                User.full_name.ilike(pattern),
                User.pinfl.ilike(pattern),
            )
        )
    if zone is not None:
        stmt = stmt.where(zone)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(User.created_at.desc(), User.id.desc()).offset(offset).limit(limit)
        )
    ).scalars()
    return list(rows), total


async def revoke_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Bulk-revokes every still-active session of `user_id` (block/delete/reset-*,
    С23): a plain UPDATE, not a per-row `service.revoke_session` call — no
    per-session audit entry, the caller's own action (`user.block` etc.) already
    covers it in the same transaction."""
    result = await db.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(Session.id)
    )
    return len(result.all())


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
