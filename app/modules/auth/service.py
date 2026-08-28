"""Auth service: sessions, login+MFA, passwords. The only door for other modules."""

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import settings_store
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
from app.modules.auth.adapters.otp_sender import get_otp_sender
from app.modules.auth.models import OtpCode, Session, User

# Timing-uniform response (user enumeration): computed once at import so the
# unknown/inactive/no-hash branch of login_password pays the same Argon2 cost
# as a real verification instead of returning early and leaking timing.
_DUMMY_HASH = hash_password("dummy-timing-equalizer")


async def issue_session(
    db: AsyncSession, user: User, *, ip: str | None, user_agent: str | None
) -> tuple[Session, str, str]:
    """Create a session row; returns (row, raw token for the cookie, csrf token)."""
    hours = await settings_store.get_int(db, "session_absolute_hours")
    token, csrf = new_token(), new_token()
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=csrf,
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
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
        max_attempts = await settings_store.get_int(db, "login_max_attempts")
        locked = new_count >= max_attempts
        if locked:
            lockout_minutes = await settings_store.get_int(db, "login_lockout_minutes")
            await repo.lock_user(db, user.id, until=now + timedelta(minutes=lockout_minutes))
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
    ttl = await settings_store.get_int(db, "mfa_token_ttl_minutes")
    await repo.add(
        db,
        OtpCode(
            code_hash=hash_token(token),
            purpose="mfa",
            user_id=user.id,
            expires_at=now + timedelta(minutes=ttl),
        ),
    )
    return token


async def verify_mfa(
    db: AsyncSession, *, mfa_token: str, code: str, ip: str | None, user_agent: str | None
) -> tuple[User, Session, str, str]:
    """TOTP step: consumes the mfa_token, opens the session.

    Denied outcomes follow the same early-commit pattern as login_password
    (ruling 2). A wrong TOTP code counts against `otp.attempts`; once that hits
    the mfa_max_attempts setting the interim token is burned (single MFA handoff
    exhausted) AND the attempt counts against the account's own
    failed_login_count/locked_until — otherwise MFA brute force would have no
    cap at all (the mfa_token itself never expires faster than 5 minutes).
    """
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
        if otp.attempts >= await settings_store.get_int(db, "mfa_max_attempts"):
            otp.used_at = now  # burn: this handoff token is spent, MFA must restart
            new_count = await repo.increment_failed_logins(db, user.id)
            locked = new_count >= await settings_store.get_int(db, "login_max_attempts")
            if locked:
                lockout_minutes = await settings_store.get_int(db, "login_lockout_minutes")
                await repo.lock_user(db, user.id, until=now + timedelta(minutes=lockout_minutes))
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


OTP_TOKEN_TTL_MINUTES = 30  # verified-target token consumed by registration/contact change


def _mask_target(target: str) -> str:
    """Audit-safe form: +99890***4567 / a***@host.

    Defense in depth: `OtpVerifyIn.target` carries schema-level format
    validation (see `schemas._validate_target_format`), but this must still
    degrade safely on its own for any short non-email string that reaches it
    — it must never echo more of the original than it hides.
    """
    if "@" in target:
        local, _, host = target.partition("@")
        return f"{local[:1]}***@{host}"
    if len(target) <= 10:
        if len(target) < 4:
            return "***"
        return f"***{target[-4:]}"
    return f"{target[:6]}***{target[-4:]}"


def _generate_otp_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


async def request_otp(
    db: AsyncSession, *, target_type: str, target: str, purpose: str, ip: str | None
) -> None:
    now = datetime.now(UTC)
    limit = await settings_store.get_int(db, "otp_hourly_limit")
    recent = await repo.count_recent_otps(
        db, target=target, purpose=purpose, since=now - timedelta(hours=1)
    )
    if recent >= limit:
        await audit.log(
            db,
            action="otp.request",
            result="denied",
            basis="rate limit",
            ip=ip,
            extra={"target": _mask_target(target), "purpose": purpose},
        )
        await db.commit()
        raise err("ERR-AUTH-009")
    code = _generate_otp_code()
    ttl = await settings_store.get_int(db, "otp_ttl_minutes")
    await repo.add(
        db,
        OtpCode(
            target_type=target_type,
            target=target,
            code_hash=hash_token(code),
            purpose=purpose,
            expires_at=now + timedelta(minutes=ttl),
        ),
    )
    await audit.log(
        db,
        action="otp.request",
        ip=ip,
        extra={"target": _mask_target(target), "purpose": purpose},
    )
    await get_otp_sender().send(target_type=target_type, target=target, code=code)


async def verify_otp(
    db: AsyncSession, *, target: str, code: str, purpose: str, ip: str | None
) -> str:
    row = await repo.latest_pending_otp(db, target=target, purpose=purpose)
    max_attempts = await settings_store.get_int(db, "otp_max_attempts")
    if row is None or row.attempts >= max_attempts:
        await audit.log(
            db,
            action="otp.verify",
            result="denied",
            basis="no valid code",
            ip=ip,
            extra={"target": _mask_target(target), "purpose": purpose},
        )
        await db.commit()
        raise err("ERR-AUTH-010")
    if not secrets.compare_digest(hash_token(code), row.code_hash):
        row.attempts += 1
        await audit.log(
            db,
            action="otp.verify",
            result="denied",
            basis="wrong code",
            ip=ip,
            extra={"target": _mask_target(target), "purpose": purpose},
        )
        await db.commit()  # the attempt counter must survive the raise (ruling 2)
        raise err("ERR-AUTH-010")
    now = datetime.now(UTC)
    row.used_at = now
    token = new_token()
    await repo.add(
        db,
        OtpCode(
            target_type=row.target_type,
            target=target,
            code_hash=hash_token(token),
            purpose=f"{purpose}_token",
            expires_at=now + timedelta(minutes=OTP_TOKEN_TTL_MINUTES),
        ),
    )
    await audit.log(
        db,
        action="otp.verify",
        ip=ip,
        extra={"target": _mask_target(target), "purpose": purpose},
    )
    return token


async def consume_otp_token(db: AsyncSession, *, token: str, purpose: str, target: str) -> None:
    """Burn a `{purpose}_token` issued by verify_otp; the token must belong to `target`."""
    row = await repo.get_valid_otp(db, hash_token(token), purpose=f"{purpose}_token")
    if row is None or row.target != target:
        raise err("ERR-AUTH-010")
    row.used_at = datetime.now(UTC)
