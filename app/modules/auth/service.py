"""Auth service: sessions, login+MFA, passwords. The only door for other modules."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto import decrypt_str
from app.core.errors import err
from app.core.security import (
    hash_password,
    hash_token,
    new_token,
    validate_password_policy,
    verify_password,
    verify_totp,
)
from app.modules.audit import service as audit
from app.modules.auth import repo
from app.modules.auth.models import OtpCode, Session, User

# Timing-uniform response (user enumeration): computed once at import so the
# unknown/inactive/no-hash branch of login_password pays the same Argon2 cost
# as a real verification instead of returning early and leaking timing.
_DUMMY_HASH = hash_password("dummy-timing-equalizer")


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


async def login_password(
    db: AsyncSession, *, login: str, password: str, ip: str | None, user_agent: str | None
) -> str:
    """Password step. Returns a 5-min single-use mfa_token (ruling 6).

    Denied outcomes follow ruling 2: counters + audit(result=denied) are
    committed explicitly BEFORE raising, so the trail survives the rollback.
    """
    settings = get_settings()
    user = await repo.get_user_by_login(db, login)
    now = datetime.now(UTC)
    if user is None or user.status != "active" or user.password_hash is None:
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        raise err("ERR-AUTH-001")  # no user to audit against; uniform response
    if user.locked_until is not None and user.locked_until > now:
        await audit.log(
            db,
            action="user.login",
            user_id=user.id,
            result="denied",
            basis="locked",
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-003")
    if not await asyncio.to_thread(verify_password, password, user.password_hash):
        new_count = await repo.increment_failed_logins(db, user.id)
        locked = new_count >= settings.login_max_attempts
        if locked:
            await repo.lock_user(
                db, user.id, until=now + timedelta(minutes=settings.login_lockout_minutes)
            )
        await audit.log(
            db,
            action="user.login",
            user_id=user.id,
            result="denied",
            basis="bad password" + (", locked" if locked else ""),
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-003" if locked else "ERR-AUTH-001")
    token = new_token()
    await repo.add(
        db,
        OtpCode(
            code_hash=hash_token(token),
            purpose="mfa",
            user_id=user.id,
            expires_at=now + timedelta(minutes=settings.mfa_token_ttl_minutes),
        ),
    )
    return token


async def verify_mfa(
    db: AsyncSession, *, mfa_token: str, code: str, ip: str | None, user_agent: str | None
) -> tuple[User, Session, str, str]:
    """TOTP step: consumes the mfa_token, opens the session.

    Denied outcomes follow the same early-commit pattern as login_password
    (ruling 2). A wrong TOTP code counts against `otp.attempts`; once that hits
    `settings.mfa_max_attempts` the interim token is burned (single MFA handoff
    exhausted) AND the attempt counts against the account's own
    failed_login_count/locked_until — otherwise MFA brute force would have no
    cap at all (the mfa_token itself never expires faster than 5 minutes).
    """
    settings = get_settings()
    otp = await repo.get_valid_otp(db, hash_token(mfa_token), purpose="mfa")
    if otp is None or otp.user_id is None:
        raise err("ERR-AUTH-001")
    user = await repo.get_user(db, otp.user_id)
    if user is None or user.status != "active" or user.mfa_secret is None:
        raise err("ERR-AUTH-001")
    now = datetime.now(UTC)
    if user.locked_until is not None and user.locked_until > now:
        # A lock acquired between the password step and this one (e.g. another
        # concurrent attempt) must be honoured even with a correct code.
        await audit.log(
            db,
            action="user.login",
            user_id=user.id,
            result="denied",
            basis="locked",
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-003")
    otp.attempts += 1
    try:
        secret = decrypt_str(user.mfa_secret)
    except InvalidToken:
        structlog.get_logger().error("mfa_secret_undecryptable", user_id=str(user.id))
        raise err("ERR-AUTH-001") from None
    if not verify_totp(secret, code):
        locked = False
        if otp.attempts >= settings.mfa_max_attempts:
            otp.used_at = now  # burn: this handoff token is spent, MFA must restart
            new_count = await repo.increment_failed_logins(db, user.id)
            locked = new_count >= settings.login_max_attempts
            if locked:
                await repo.lock_user(
                    db, user.id, until=now + timedelta(minutes=settings.login_lockout_minutes)
                )
            basis = "bad totp, token burned" + (", locked" if locked else "")
        else:
            basis = "bad totp"
        await audit.log(
            db,
            action="user.login",
            user_id=user.id,
            result="denied",
            basis=basis,
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-003" if locked else "ERR-AUTH-001")
    otp.used_at = now
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    row, token, csrf = await issue_session(db, user, ip=ip, user_agent=user_agent)
    await audit.log(
        db,
        action="user.login",
        user_id=user.id,
        object_type="session",
        object_id=row.id,
        ip=ip,
        user_agent=user_agent,
    )
    return user, row, token, csrf


async def change_password(
    db: AsyncSession, user: User, *, old: str, new: str, current_session_id: uuid.UUID
) -> None:
    if user.password_hash is None or not await asyncio.to_thread(
        verify_password, old, user.password_hash
    ):
        raise err("ERR-AUTH-001")
    validate_password_policy(new)
    user.password_hash = await asyncio.to_thread(hash_password, new)
    user.must_change_password = False
    for other in await repo.other_active_sessions(db, user.id, exclude=current_session_id):
        await revoke_session(db, other, reason="password change")
    await audit.log(db, action="user.password_change", user_id=user.id)
