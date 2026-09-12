"""Auth service: sessions, login+MFA, passwords. The only door for other modules."""

import asyncio
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import settings_store
from app.core.crypto import decrypt_str
from app.core.errors import err
from app.core.models import MediaFile
from app.core.schemas import LOCALES
from app.core.security import (
    hash_otp,
    hash_password,
    hash_token,
    new_token,
    validate_password_policy,
    verify_password,
    verify_totp,
)
from app.core.time import business_today
from app.modules.audit import service as audit
from app.modules.auth import repo
from app.modules.auth.models import (
    APPLICANT_ROLE_CODE,
    Applicant,
    OtpCode,
    Representation,
    Role,
    Session,
    User,
    UserConsent,
)
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.eimzo import (
    EimzoCall,
    EimzoError,
    EimzoIdentity,
    get_eimzo_adapter,
)
from app.modules.integrations.adapters.oneid import (
    OneIdCall,
    OneIdError,
    get_oneid_adapter,
)

# Timing-uniform response (user enumeration): computed once at import so the
# unknown/inactive/no-hash branch of login_password pays the same Argon2 cost
# as a real verification instead of returning early and leaking timing.
_DUMMY_HASH = hash_password("dummy-timing-equalizer")

# Mirrors `users.pinfl`'s own `CheckConstraint` (`models.py`,
# `ck_users_pinfl_format`) — checked in Python BEFORE the insert in
# `login_via_eimzo` (finding 6, final review), not left to the database to
# reject as an uncaught `IntegrityError`/500.
_PINFL_RE = re.compile(r"^[0-9]{14}$")


async def issue_session(
    db: AsyncSession,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
    oneid_access_token: str | None = None,
) -> tuple[Session, str, str]:
    """Create a session row; returns (row, raw token for the cookie, csrf token).

    `oneid_access_token` is supplied only by the OneID login and is what
    `logout_session` hands to `one_log_out`; every other caller leaves it
    None."""
    hours = await settings_store.get_int(db, "session_absolute_hours")
    token, csrf = new_token(), new_token()
    row = Session(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=csrf,
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
        ip=ip,
        user_agent=user_agent,
        oneid_access_token=oneid_access_token,
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


async def login_or_create_by_pinfl(
    db: AsyncSession,
    *,
    pinfl: str,
    full_name: str,
    method: str,
    snapshot: dict[str, Any] | None,
    phone: str | None,
    ip: str | None,
    user_agent: str | None,
    oneid_access_token: str | None = None,
) -> tuple[User, Session, str, str]:
    """Shared core of OneID/E-IMZO logins (ruling 5): any-role entry by pinfl,
    auto-creating an applicant account on first contact.

    `oneid_access_token` reaches the session row so our logout can end the
    OneID session too; the E-IMZO caller leaves it None."""
    user = await repo.get_user_by_pinfl(db, pinfl)
    now = datetime.now(UTC)
    if user is not None and user.status != "active":
        await audit.log(
            db,
            action="user.login",
            user_id=user.id,
            result="denied",
            basis=f"{method}: user not active",
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-001")
    if user is None:
        role = await repo.get_role_by_code(db, "applicant")
        assert role is not None  # seeded by migration 0003
        user = User(
            full_name=full_name,
            role_id=role.id,
            pinfl=pinfl,
            phone=phone,  # draft from the provider; verified only via OTP (С2)
        )
        await repo.add(db, user)
        await audit.log(
            db,
            action="user.create",
            user_id=user.id,
            object_type="user",
            object_id=user.id,
            basis="self-registration",
            extra={"method": method},
            ip=ip,
            user_agent=user_agent,
        )
    if snapshot is not None:
        user.oneid_profile = snapshot
    user.last_login_at = now
    row, token, csrf = await issue_session(
        db, user, ip=ip, user_agent=user_agent, oneid_access_token=oneid_access_token
    )
    await audit.log(
        db,
        action="user.login",
        user_id=user.id,
        extra={"method": method},
        ip=ip,
        user_agent=user_agent,
    )
    return user, row, token, csrf


async def _log_oneid_calls(db: AsyncSession, calls: tuple[OneIdCall, ...]) -> None:
    """One `integration_log` row per provider round trip (tz/09: logging per
    external message).

    The rows are written HERE rather than in the adapter because this is where
    the session lives — no adapter in this codebase opens a transaction of its
    own. `meta` carries the provider's own refusal codes and nothing else:
    `CLIENT_SECRET_NOT_FOUND` in the log is the difference between an
    administrator fixing one `.env` line and an administrator waiting out an
    outage that is not happening (decision #140 ruling 5). Everything else
    OneID exchanges — the PINFL, the phone, the token — is personal data or a
    credential and never reaches a log row."""
    for call in calls:
        await integrations_service.log_integration(
            db,
            direction="out",
            system="oneid",
            endpoint=call.endpoint,
            http_status=call.http_status,
            duration_ms=call.duration_ms,
            meta=(
                {"provider_message": call.provider_message, "provider_error": call.provider_error}
                if call.provider_error or call.provider_message
                else None
            ),
        )


async def _log_eimzo_calls(db: AsyncSession, calls: tuple[EimzoCall, ...]) -> None:
    """One `integration_log` row per provider round trip, mirroring
    `_log_oneid_calls` above (stage 5.1 task 6) — the same shape, a different
    provider.

    `calls` is whatever `RealEimzo.calls` accumulated on the ONE adapter
    instance a caller used, whatever happened — success, a refused challenge
    or signature, or a transport failure (`EimzoCall`'s own docstring). A
    caller reaches this AFTER catching `EimzoError` too, passing
    `getattr(adapter, "calls", ())`: `MockEimzo` has no `.calls` attribute at
    all (no round trip was ever made), and this loop then simply does
    nothing. `meta` carries only `EimzoCall.provider_status`/
    `provider_message` — the provider's own numeric status and its own
    message, nothing else: never a signed challenge, never a PINFL, never a
    certificate subject."""
    for call in calls:
        await integrations_service.log_integration(
            db,
            direction="out",
            system="eimzo",
            endpoint=call.endpoint,
            http_status=call.http_status,
            duration_ms=call.duration_ms,
            meta=(
                {"provider_status": call.provider_status, "provider_message": call.provider_message}
                if call.provider_status is not None or call.provider_message is not None
                else None
            ),
        )


async def login_via_oneid(
    db: AsyncSession, *, code: str, ip: str | None, user_agent: str | None
) -> tuple[User, Session, str, str]:
    adapter = get_oneid_adapter()
    try:
        login = await adapter.exchange_code(code)
    except OneIdError as exc:
        # The early-commit pattern this codebase uses for denied outcomes: the
        # raise below rolls the session back, and a failed login is precisely
        # the case the log exists for. Nothing else is pending here — the
        # exchange is the first thing this function does.
        await _log_oneid_calls(db, exc.calls)
        await db.commit()
        raise err(exc.err_code) from exc
    await _log_oneid_calls(db, login.calls)
    profile = login.profile
    return await login_or_create_by_pinfl(
        db,
        pinfl=profile.pinfl,
        full_name=profile.full_name,
        method="oneid",
        snapshot=profile.to_snapshot(),
        phone=profile.phone,
        ip=ip,
        user_agent=user_agent,
        oneid_access_token=login.access_token,
    )


EIMZO_CHALLENGE_TTL_MINUTES = 5


async def issue_eimzo_challenge(
    db: AsyncSession, *, ip: str | None = None, mode: str | None = None
) -> str:
    """Plan 05.2 ruling R1 (option «а»): in `real` mode the login challenge
    belongs to e-imzo-server, not to us. `POST /frontend/challenge` mints it
    there with its own 120-second TTL and matches it again itself inside
    `/backend/auth` — our own `otp_codes` copy would be a second TTL and a
    second way to fail, protecting nothing (their server refuses a signature
    whose challenge it does not recognise regardless of what we stored). So in
    `real` mode this proxies the provider's own challenge straight through and
    writes nothing of ours; `login_via_eimzo` mirrors this by skipping the
    `otp_codes` lookup for the same mode (see its own comment).

    In `mock` mode nothing changes: we still mint and store our own token
    exactly as before this ruling.

    `mode` defaults to `get_settings().eimzo_mode` — the route never passes
    it, and it exists as a parameter only so a test can force either branch
    without needing `get_eimzo_adapter()` to actually be mode-aware (a stub
    adapter answers the same regardless of the configured mode)."""
    if mode is None:
        mode = get_settings().eimzo_mode
    if mode == "real":
        adapter = get_eimzo_adapter()
        try:
            challenge = await adapter.issue_challenge(ip=ip)
        except EimzoError as exc:
            # Task 5: the log is written on a refusal too -- an
            # administrator must be able to tell "our configuration is
            # wrong" from "the provider is down", and nothing else of ours
            # is pending here (this is the first thing the real-mode branch
            # does), so the commit only persists this one log row.
            await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
            await db.commit()
            raise err(exc.err_code) from exc
        await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
        return challenge
    challenge = new_token()
    await repo.add(
        db,
        OtpCode(
            code_hash=hash_token(challenge),
            purpose="eimzo_challenge",
            expires_at=datetime.now(UTC) + timedelta(minutes=EIMZO_CHALLENGE_TTL_MINUTES),
        ),
    )
    return challenge


async def login_via_eimzo(
    db: AsyncSession, *, signed_challenge: str, ip: str | None, user_agent: str | None
) -> tuple[User, Session, str, str]:
    adapter = get_eimzo_adapter()
    try:
        identity = await adapter.verify_signed_challenge(signed_challenge, ip=ip)
    except EimzoError as exc:
        # Task 5: log the refused round trip before it is rolled back --
        # nothing else of ours has been written yet (this is the first thing
        # `login_via_eimzo` does), so the commit only persists this one row.
        await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
        await db.commit()
        # Minor 9 (final review): carry the provider's own status/reason
        # instead of a bare 502/503 -- `integrations.service.
        # eimzo_error_details` already builds exactly this payload for the
        # timestamp route; reused here rather than a second copy.
        raise err(exc.err_code, details=integrations_service.eimzo_error_details(exc)) from exc
    await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
    # Ruling R1: in `real` mode e-imzo-server has ALREADY matched the challenge
    # itself, inside `/backend/auth`, before ever answering `status: 1` — and
    # `issue_eimzo_challenge` never wrote an `otp_codes` row for it in this
    # mode (see that function's own comment). `identity.challenge` is also
    # deliberately `""` here (`RealEimzo.verify_signed_challenge`'s own
    # docstring), so looking it up would always miss and fail closed on every
    # real login. Do NOT "fix" this back into an unconditional lookup — that
    # is exactly the regression this comment exists to prevent.
    if get_settings().eimzo_mode == "mock":
        row = await repo.get_valid_otp(
            db, hash_token(identity.challenge), purpose="eimzo_challenge"
        )
        if row is None:
            raise err("ERR-AUTH-004")  # unknown, expired or replayed challenge
        row.used_at = datetime.now(UTC)
    if identity.cert_expires_at is not None and identity.cert_expires_at <= datetime.now(UTC):
        await audit.log(
            db,
            action="user.login",
            result="denied",
            basis="eimzo: certificate expired",
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-004")
    if not _PINFL_RE.fullmatch(identity.pinfl):
        # Finding 6 (final review): `eimzo_wire.read_subject`'s STIR/""
        # fallback is right for an OWNERSHIP check (`signatures.
        # _ownership_reason`, which only needs SOME identifier to compare
        # against) and wrong for LOGIN -- a legal-entity-only certificate
        # the provider accepts with `status: 1` hands `identity.pinfl` a
        # 9-digit STIR, or `""`, neither of which `login_or_create_by_pinfl`
        # can insert into `users.pinfl` (`CheckConstraint`, 14 digits only).
        # Refuse here, cleanly, before that insert -- not an uncaught
        # `IntegrityError` turning into a bare 500.
        await audit.log(
            db,
            action="user.login",
            result="denied",
            basis="eimzo: no personal pinfl",
            ip=ip,
            user_agent=user_agent,
        )
        await db.commit()
        raise err("ERR-AUTH-004")
    return await login_or_create_by_pinfl(
        db,
        pinfl=identity.pinfl,
        full_name=identity.full_name,
        method="eimzo",
        snapshot=None,
        phone=None,
        ip=ip,
        user_agent=user_agent,
    )


async def logout_session(db: AsyncSession, session_row: Session) -> None:
    """Revoke OUR session first, then ask OneID to end its own.

    The order is the design. The provider call is best effort and may hang for
    the adapter's whole timeout, while a citizen who pressed "sign out" must be
    signed out of our system whatever OneID does — so the revocation is
    unconditional and the `one_log_out` failure is only logged (the adapter
    itself already swallows a provider error; this catch is for the day one
    stops).

    Without the provider call our logout would close only our own session: the
    OneID session survives in the browser, and on a shared computer the next
    person's "sign in with OneID" would land in this citizen's cabinet with no
    password (decision #140 ruling 4)."""
    token = session_row.oneid_access_token
    session_row.oneid_access_token = None
    await revoke_session(db, session_row, reason="logout")
    if not token:
        return
    try:
        await get_oneid_adapter().logout(token)
    except OneIdError as exc:
        structlog.get_logger().warning("oneid.logout_failed", err_code=exc.err_code)


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


@dataclass(frozen=True)
class PasswordStep:
    """What the password step produced — exactly one of the two is set.

    `mfa_token` while the second factor is required: the caller must still pass
    it to `verify_mfa`. `session` when `mfa_enabled` is off: the session already
    exists and the caller only has to set its cookies. The two are not
    interchangeable, and a caller that reads the wrong one gets None rather than
    a half-open login.
    """

    mfa_token: str | None = None
    session: tuple[User, Session, str, str] | None = None


async def _complete_login(
    db: AsyncSession,
    user: User,
    *,
    ip: str | None,
    user_agent: str | None,
    basis: str | None = None,
) -> tuple[User, Session, str, str]:
    """The tail every password login shares: clear the lockout counters, stamp
    the login, open the session, audit it.

    Extracted so the with-MFA and without-MFA paths cannot drift — a login that
    forgot to reset `failed_login_count` would leave the account one bad
    password away from a lockout it had already cleared."""
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(UTC)
    row, token, csrf = await issue_session(db, user, ip=ip, user_agent=user_agent)
    await audit.log(
        db,
        action="user.login",
        user_id=user.id,
        object_type="session",
        object_id=row.id,
        basis=basis,
        ip=ip,
        user_agent=user_agent,
    )
    return user, row, token, csrf


async def login_password(
    db: AsyncSession, *, login: str, password: str, ip: str | None, user_agent: str | None
) -> PasswordStep:
    """Password step. Returns a 5-min single-use mfa_token (ruling 6) — or, while
    `mfa_enabled` is off, the finished session itself.

    Denied outcomes follow ruling 2: counters + audit(result=denied) are
    committed explicitly BEFORE raising, so the trail survives the rollback.

    The switch removes the SECOND factor only: every guard below — unknown user,
    inactive, locked, wrong password, and the constant-time dummy verification
    that hides which of those it was — runs exactly as it does with MFA on.
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
    if not await settings_store.get_bool(db, "mfa_enabled"):
        # WARNING, not info: while the switch is off this line is the only thing
        # in the process log that says the deployment runs on one factor. The
        # audit row carries the same fact for the trail that outlives the log.
        structlog.get_logger().warning("mfa_disabled_login", user_id=str(user.id), login=user.login)
        return PasswordStep(
            session=await _complete_login(
                db, user, ip=ip, user_agent=user_agent, basis="mfa disabled"
            )
        )
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
    return PasswordStep(mfa_token=token)


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

    Nothing here is relaxed while `mfa_enabled` is off — that switch is read one
    step earlier, and with it off no mfa_token is ever minted, so this route
    simply has nothing valid to consume.
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
    return await _complete_login(db, user, ip=ip, user_agent=user_agent)


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
            code_hash=hash_otp(code),
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
    await integrations_service.enqueue(
        db,
        destination="sms_otp",
        payload={"target_type": target_type, "target": target, "code": code},
    )


async def _burn_otp_code(
    db: AsyncSession, *, target: str, code: str, purpose: str, ip: str | None
) -> OtpCode:
    """Check `code` against the latest pending OTP for `target`/`purpose` and
    mark it used. Denied paths commit the attempt counter and the audit row
    before raising (ruling 2). Shared by `verify_otp` (which then mints a
    `{purpose}_token`) and the self-service password reset (which does not)."""
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
    if not secrets.compare_digest(hash_otp(code), row.code_hash):
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
    row.used_at = datetime.now(UTC)
    await audit.log(
        db,
        action="otp.verify",
        ip=ip,
        extra={"target": _mask_target(target), "purpose": purpose},
    )
    return row


async def verify_otp(
    db: AsyncSession, *, target: str, code: str, purpose: str, ip: str | None
) -> str:
    row = await _burn_otp_code(db, target=target, code=code, purpose=purpose, ip=ip)
    token = new_token()
    await repo.add(
        db,
        OtpCode(
            target_type=row.target_type,
            target=target,
            code_hash=hash_token(token),
            purpose=f"{purpose}_token",
            expires_at=datetime.now(UTC) + timedelta(minutes=OTP_TOKEN_TTL_MINUTES),
        ),
    )
    return token


# ---- self-service password reset (decision #208, supersedes the admin-only
# ---- half of #32) -----------------------------------------------------------

PASSWORD_RESET_PURPOSE = "password_reset"


def mask_phone_for_display(phone: str) -> str:
    """`+998901234567` → `+998 ** *** ** 67`: the country code and the last two
    digits, enough to recognise one's own number and nothing more."""
    return f"{phone[:4]} ** *** ** {phone[-2:]}"


def mask_email_for_display(email: str) -> str:
    return _mask_target(email)


def _self_reset_user(user: User | None) -> User | None:
    """Only an active staff account with a password can reset it by itself;
    applicants (no password, decision #32) and blocked accounts answer as if
    the login did not exist."""
    if user is None or user.status != "active" or user.password_hash is None:
        return None
    return user


def _reset_contact(user: User, channel: str) -> str | None:
    return user.phone if channel == "phone" else user.email


async def forgot_password_lookup(
    db: AsyncSession, *, login: str, ip: str | None
) -> tuple[str | None, str | None]:
    """Masked (phone, email) of the login's owner — `None` where the card has
    no such contact. An unknown or ineligible login gets `(None, None)`, the
    same answer as a card with nothing filled in, so the existence oracle this
    route unavoidably is (decision #208) says as little as it can."""
    user = _self_reset_user(await repo.get_user_by_login(db, login))
    if user is None:
        await audit.log(
            db,
            action="user.password_forgot",
            result="denied",
            basis="unknown login",
            ip=ip,
            extra={"login": login[:64]},
        )
        return None, None
    await audit.log(db, action="user.password_forgot", user_id=user.id, ip=ip)
    return (
        mask_phone_for_display(user.phone) if user.phone else None,
        mask_email_for_display(user.email) if user.email else None,
    )


async def forgot_password_send(
    db: AsyncSession, *, login: str, channel: str, ip: str | None
) -> None:
    """Send a reset code to the login's own phone or e-mail. The target is
    read from the card, never from the request, so a caller learns nothing
    the lookup did not already say. Per-target hourly cap, hashing and outbox
    delivery are `request_otp`'s."""
    user = _self_reset_user(await repo.get_user_by_login(db, login))
    target = _reset_contact(user, channel) if user is not None else None
    if user is None or target is None:
        raise err("ERR-AUTH-001")
    await request_otp(db, target_type=channel, target=target, purpose=PASSWORD_RESET_PURPOSE, ip=ip)


async def forgot_password_reset(
    db: AsyncSession, *, login: str, channel: str, code: str, new_password: str, ip: str | None
) -> None:
    """Burn the code and set the new password. The policy is checked BEFORE
    the code so a too-short password does not cost the user a fresh SMS.
    Every session is revoked and the lockout cleared — the person who just
    proved control of the contact is the owner, and a lockout left over from
    someone else's guessing would keep them out of their reset account."""
    user = _self_reset_user(await repo.get_user_by_login(db, login))
    target = _reset_contact(user, channel) if user is not None else None
    if user is None or target is None:
        raise err("ERR-AUTH-001")
    validate_password_policy(new_password)
    await _burn_otp_code(db, target=target, code=code, purpose=PASSWORD_RESET_PURPOSE, ip=ip)
    user.password_hash = await asyncio.to_thread(hash_password, new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    await repo.revoke_user_sessions(db, user.id)
    await audit.log(
        db, action="user.password_reset_self", user_id=user.id, ip=ip, extra={"channel": channel}
    )


async def consume_otp_token(db: AsyncSession, *, token: str, purpose: str, target: str) -> None:
    """Burn a `{purpose}_token` issued by verify_otp; the token must belong to `target`."""
    row = await repo.get_valid_otp(db, hash_token(token), purpose=f"{purpose}_token")
    if row is None or row.target != target:
        raise err("ERR-AUTH-010")
    row.used_at = datetime.now(UTC)


async def complete_registration(
    db: AsyncSession,
    user: User,
    *,
    privacy_policy_version: str,
    offer_version: str,
    phone: str,
    otp_token: str,
    email: str | None,
    region_id: uuid.UUID | None,
    district_id: uuid.UUID | None,
    address: str | None,
    ip: str | None,
) -> Applicant:
    if await repo.role_code(db, user) != APPLICANT_ROLE_CODE:
        raise err("ERR-ACL-001", details={"reason": "not an applicant account"})
    if await repo.get_own_applicant(db, user.id) is not None:
        raise err("ERR-AUTH-012")
    assert user.pinfl is not None  # oneid/eimzo entry always sets it
    current_privacy = await settings_store.get_str(db, "privacy_policy_version")
    current_offer = await settings_store.get_str(db, "offer_version")
    stale = {}
    if privacy_policy_version != current_privacy:
        stale["privacy_policy"] = current_privacy
    if offer_version != current_offer:
        stale["offer"] = current_offer
    if stale:
        raise err("ERR-VAL-001", details={"consents_current": stale})
    await consume_otp_token(db, token=otp_token, purpose="phone_verify", target=phone)
    now = datetime.now(UTC)
    applicant = Applicant(
        kind="individual",
        pinfl=user.pinfl,
        name=user.full_name,
        phone=phone,
        email=email,
        region_id=region_id,
        district_id=district_id,
        address=address,
        owner_user_id=user.id,
        verified_at=now if user.oneid_profile is not None else None,
        verify_source="oneid" if user.oneid_profile is not None else None,
    )
    await repo.add(db, applicant)
    for doc_type, doc_version in (
        ("privacy_policy", privacy_policy_version),
        ("offer", offer_version),
    ):
        await repo.add(
            db, UserConsent(user_id=user.id, doc_type=doc_type, doc_version=doc_version, ip=ip)
        )
    user.phone = phone
    user.phone_verified_at = now
    if email is not None:
        user.email = email  # verified later via PATCH /auth/me (ruling 11)
    await audit.log(
        db,
        action="applicant.register",
        user_id=user.id,
        object_type="applicant",
        object_id=applicant.id,
        ip=ip,
    )
    return applicant


async def _verify_org_challenge(
    db: AsyncSession, *, signed_challenge: str, stir: str, signer_pinfl: str, ip: str | None
) -> EimzoIdentity:
    """org_eri basis: a fresh org-cert signature naming this stir and this signer.

    Gap closed here, found in Task 4's own review: this function used to look
    up `identity.challenge` in `otp_codes` UNCONDITIONALLY, the same mistake
    `login_via_eimzo` had before ruling R1. In `real` mode e-imzo-server has
    ALREADY matched its own challenge, inside `/backend/auth`, before ever
    answering `status: 1` -- and `identity.challenge` is deliberately `""` in
    that mode (`RealEimzo.verify_signed_challenge`'s own docstring), so an
    unconditional lookup here always misses and refuses every legal-entity
    attach the moment `EIMZO_MODE=real` is set, forever. Do NOT "fix" this
    back into an unconditional lookup -- that is exactly the regression this
    comment exists to prevent."""
    adapter = get_eimzo_adapter()
    try:
        identity = await adapter.verify_signed_challenge(signed_challenge, ip=ip)
    except EimzoError as exc:
        # Fix round 1, finding 3: an integration error (`ERR-INT-001`/
        # `ERR-INT-002`, a provider outage or a bad response) is not a
        # verdict about the CERTIFICATE at all -- collapsing it into
        # `ERR-ACL-001` used to tell a citizen their org certificate was
        # invalid when the real story was that E-IMZO could not be reached.
        # Only the genuine login-contract refusal (`ERR-AUTH-004`, the
        # unchanged status the mock and `RealEimzo.verify_signed_challenge`
        # both raise for a non-1 status) becomes the ACL error here; every
        # other code keeps its own.
        #
        # Task 5: the refused round trip is logged before it is rolled back
        # -- nothing else of ours is pending here (`attach_legal`/
        # `add_representation` only read before calling this), so the commit
        # only persists this one log row.
        await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
        await db.commit()
        if exc.err_code != "ERR-AUTH-004":
            # Minor 9 (final review): carry the provider's own status/reason
            # for the two codes that have one (`ERR-INT-001`/`ERR-INT-002`)
            # instead of a bare 502/503 -- reuses `integrations.service.
            # eimzo_error_details` rather than a second copy.
            raise err(exc.err_code, details=integrations_service.eimzo_error_details(exc)) from exc
        raise err("ERR-ACL-001", details={"basis": "org_eri", "reason": "bad signature"}) from exc
    await _log_eimzo_calls(db, getattr(adapter, "calls", ()))
    # Gap fix (this function's own docstring): in `real` mode the provider
    # already owns and matched the challenge, and `identity.challenge` is
    # always `""` -- looking it up here would always miss.
    if get_settings().eimzo_mode == "mock":
        row = await repo.get_valid_otp(
            db, hash_token(identity.challenge), purpose="eimzo_challenge"
        )
        if row is None:
            raise err("ERR-ACL-001", details={"basis": "org_eri", "reason": "challenge invalid"})
        row.used_at = datetime.now(UTC)
    if identity.tin != stir or identity.pinfl != signer_pinfl:
        raise err("ERR-ACL-001", details={"basis": "org_eri", "reason": "certificate mismatch"})
    return identity


def _director_listed(user: User, stir: str) -> tuple[bool, str | None]:
    """director_registry basis: OneID's legal_info is the directors' registry (ruling 12)."""
    profile = user.oneid_profile or {}
    for entry in profile.get("legal_info", []):
        if entry.get("le_tin") == stir:
            return True, entry.get("le_name")
    return False, None


async def _check_poa_file(db: AsyncSession, *, file_id: uuid.UUID, actor: User) -> None:
    """ruling 6 (3.3b): poa file must exist, be active, own, and be a PDF."""
    file = await db.get(MediaFile, file_id)
    if file is None or file.status != "active":
        raise err("ERR-VAL-001", details={"reason": "poa_file_not_found"})
    if file.uploaded_by != actor.id:
        raise err("ERR-VAL-001", details={"reason": "poa_file_not_owned"})
    if file.content_type != "application/pdf":
        raise err("ERR-VAL-001", details={"reason": "poa_file_not_pdf"})


async def attach_legal(
    db: AsyncSession,
    user: User,
    *,
    stir: str,
    basis: str,
    signed_challenge: str | None,
    poa_file_id: uuid.UUID | None,
    valid_until: date | None,
    name: str | None,
    ip: str | None,
) -> tuple[Applicant, Representation]:
    if await repo.role_code(db, user) != APPLICANT_ROLE_CODE:
        raise err("ERR-ACL-001", details={"reason": "not an applicant account"})
    assert user.pinfl is not None
    legal_name = name
    requisites: dict[str, Any] | None = None
    if basis == "org_eri":
        assert signed_challenge is not None  # schema guarantees
        identity = await _verify_org_challenge(
            db, signed_challenge=signed_challenge, stir=stir, signer_pinfl=user.pinfl, ip=ip
        )
        legal_name = identity.legal_name or legal_name or f"STIR {stir}"
        requisites = {"cert_serial": identity.cert_serial}
    elif basis == "director_registry":
        listed, le_name = _director_listed(user, stir)
        if not listed:
            raise err(
                "ERR-ACL-001",
                details={"basis": "director_registry", "reason": "stir not in oneid profile"},
            )
        legal_name = le_name or legal_name or f"STIR {stir}"
    else:  # poa — schema guarantees file/term/name
        assert legal_name is not None
        assert poa_file_id is not None
        await _check_poa_file(db, file_id=poa_file_id, actor=user)
    applicant = await repo.get_applicant_by_stir(db, stir)
    created = False
    if applicant is None:
        applicant = Applicant(
            kind="legal",
            stir=stir,
            name=legal_name,
            requisites=requisites,
            verified_at=datetime.now(UTC) if basis != "poa" else None,
            verify_source=basis if basis != "poa" else None,
        )
        await repo.add(db, applicant)
        created = True
    existing = await repo.get_effective_representation(
        db, applicant_id=applicant.id, user_id=user.id, today=business_today()
    )
    if existing is not None:
        raise err("ERR-AUTH-011")
    # A poa attach can pre-create an applicant row for any stir under an arbitrary
    # name (basis=poa never verifies the stir against anything) — the first
    # cryptographically/registry-verified attach for that stir heals the row instead
    # of silently inheriting the squatted name. Only on the success path (a raise
    # above would roll the heal back too, which is fine — nothing urgent to fix on a
    # duplicate/denied attach).
    healed = (
        not created and basis in ("org_eri", "director_registry") and applicant.verified_at is None
    )
    if healed:
        applicant.name = legal_name
        applicant.verified_at = datetime.now(UTC)
        applicant.verify_source = basis
        if basis == "org_eri":
            applicant.requisites = requisites
    representation = Representation(
        applicant_id=applicant.id,
        user_id=user.id,
        basis=basis,
        poa_file_id=poa_file_id,
        valid_from=business_today(),
        valid_until=valid_until,
    )
    await repo.add(db, representation)
    if created:
        await audit.log(
            db,
            action="applicant.create_legal",
            user_id=user.id,
            object_type="applicant",
            object_id=applicant.id,
            extra={"stir": stir},
            ip=ip,
        )
    if healed:
        await audit.log(
            db,
            action="applicant.verify",
            user_id=user.id,
            object_type="applicant",
            object_id=applicant.id,
            extra={"stir": stir, "source": basis},
            ip=ip,
        )
    await audit.log(
        db,
        action="representation.create",
        user_id=user.id,
        object_type="representation",
        object_id=representation.id,
        basis=basis,
        ip=ip,
    )
    return applicant, representation


async def add_representation(
    db: AsyncSession,
    user: User,
    *,
    applicant_id: uuid.UUID,
    user_pinfl: str,
    basis: str,
    signed_challenge: str | None,
    poa_file_id: uuid.UUID | None,
    valid_until: date | None,
    ip: str | None,
) -> tuple[Representation, Applicant]:
    applicant = await db.get(Applicant, applicant_id)
    if applicant is None or applicant.kind != "legal":
        raise err("ERR-SYS-003")
    today = business_today()
    own = await repo.get_effective_representation(
        db, applicant_id=applicant_id, user_id=user.id, today=today
    )
    if own is None or own.basis not in ("org_eri", "director_registry"):
        raise err("ERR-ACL-001", details={"reason": "org_eri or director basis required"})
    candidate = await repo.get_user_by_pinfl(db, user_pinfl)
    if candidate is None or await repo.get_own_applicant(db, candidate.id) is None:
        raise err("ERR-SYS-003", details={"reason": "candidate must sign in and register first"})
    if await repo.role_code(db, candidate) != APPLICANT_ROLE_CODE:
        raise err("ERR-ACL-001", details={"reason": "candidate is not an applicant account"})
    assert applicant.stir is not None and user.pinfl is not None
    if basis == "org_eri":
        assert signed_challenge is not None
        await _verify_org_challenge(
            db,
            signed_challenge=signed_challenge,
            stir=applicant.stir,
            signer_pinfl=user.pinfl,
            ip=ip,
        )
    elif basis == "director_registry":
        listed, _ = _director_listed(candidate, applicant.stir)
        if not listed:
            raise err(
                "ERR-ACL-001",
                details={"basis": "director_registry", "reason": "candidate not listed"},
            )
    elif basis == "poa":
        assert poa_file_id is not None  # schema guarantees
        await _check_poa_file(db, file_id=poa_file_id, actor=user)
    existing = await repo.get_effective_representation(
        db, applicant_id=applicant_id, user_id=candidate.id, today=today
    )
    if existing is not None:
        raise err("ERR-AUTH-011")
    representation = Representation(
        applicant_id=applicant_id,
        user_id=candidate.id,
        basis=basis,
        poa_file_id=poa_file_id,
        valid_from=today,
        valid_until=valid_until,
    )
    await repo.add(db, representation)
    await audit.log(
        db,
        action="representation.create",
        user_id=user.id,
        object_type="representation",
        object_id=representation.id,
        basis=basis,
        extra={"for_user": str(candidate.id)},
        ip=ip,
    )
    return representation, applicant


async def has_effective_representation(db: AsyncSession, *, user_id: uuid.UUID, stir: str) -> bool:
    """Pass-through to `repo.get_effective_representation`, resolved from a STIR rather
    than an `applicant_id` — the public entry point another module (`signatures`, proving
    an organisation certificate belongs to its presenter) needs instead of reaching into
    `auth.repo` directly, which the module-boundary rule (backend/CLAUDE.md: cross-module
    calls only via the other module's service) forbids. "Effective" means exactly what
    `attach_legal`/`add_representation` above already mean by it: `status='active'` and
    not past `valid_until`, judged against `business_today()`, never `date.today()`
    (lesson). No `applicants` row for `stir` at all is simply "no representation"."""
    applicant = await repo.get_applicant_by_stir(db, stir)
    if applicant is None:
        return False
    representation = await repo.get_effective_representation(
        db, applicant_id=applicant.id, user_id=user_id, today=business_today()
    )
    return representation is not None


async def has_effective_representation_of(
    db: AsyncSession, *, user_id: uuid.UUID, applicant_id: uuid.UUID
) -> bool:
    """Pass-through to `repo.get_effective_representation`, resolved directly
    from an `applicant_id` — for a caller that already holds it
    (`payments.service`, deciding whether a representative may see or pay a
    LEGAL applicant's invoice) and has no STIR to look up the way
    `has_effective_representation` above does. Same "effective" meaning:
    `status='active'` and not past `valid_until`, judged against
    `business_today()`, never `date.today()` (lesson). First consumer:
    3.10a task 5's ownership ruling — a legal entity's non-owner
    representative must be able to act on an invoice they filed themselves,
    not just its `owner_user_id` (which is `None` for `kind='legal'`
    anyway)."""
    representation = await repo.get_effective_representation(
        db, applicant_id=applicant_id, user_id=user_id, today=business_today()
    )
    return representation is not None


async def effective_representation_of(
    db: AsyncSession, *, user_id: uuid.UUID, applicant_id: uuid.UUID
) -> Representation | None:
    """The ROW behind `has_effective_representation_of` above, for a caller that
    has to STORE which power of attorney it acted under rather than merely check
    that one exists — `applications.service._build_filing` fills
    `applications.representation_id`, the column that says on whose authority a
    representative filed for a legal entity.

    Same "effective" meaning as every sibling here: `status='active'` and not
    past `valid_until`, judged against `business_today()`, never `date.today()`
    (lesson). Same repo call as the boolean sibling, so the two can never
    disagree about which representation is the effective one."""
    return await repo.get_effective_representation(
        db, applicant_id=applicant_id, user_id=user_id, today=business_today()
    )


async def effective_representative(db: AsyncSession, applicant_id: uuid.UUID) -> uuid.UUID | None:
    """One user currently holding an EFFECTIVE representation of a LEGAL
    applicant, or `None` if nobody does. "Effective" means the same thing as
    everywhere else in this file: `status='active'` and not past
    `valid_until`, judged against `business_today()`, never `date.today()`
    (lesson). A legal entity has SEVERAL representatives (decision #9); this
    is not "the primary one" (no rule names one), only "a currently valid
    one", deterministic via `repo.any_effective_representative`'s own
    ordering.

    First caller: `inspections.service._violator_recipient` (ruling R2) — a
    violation case is opened BY THE SYSTEM when an inspector signs an act,
    so unlike `applications`/`permits` there is no `submitted_by_user_id` to
    fall back to when the applicant itself has no account."""
    return await repo.any_effective_representative(db, applicant_id, business_today())


async def get_own_applicant(db: AsyncSession, user_id: uuid.UUID) -> Applicant | None:
    """The `Applicant` this user itself owns (`Applicant.owner_user_id`), or
    `None`. Thin pass-through to `repo.get_own_applicant` — kept here, not
    called directly, because the module-boundary rule (backend/CLAUDE.md:
    cross-module calls only via the other module's service) forbids another
    module reaching into `auth.repo` itself. First consumer: `payments.service`
    resolving "is this caller the invoice's own applicant" (3.10a task 2) —
    matched on `Applicant.id`, never on which user happened to submit a given
    application, since a legal entity's `Applicant` row is shared by several
    representatives."""
    return await repo.get_own_applicant(db, user_id)


async def own_applicant_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Every `applicants` row this user may act for TODAY: their own individual
    row (`applicants.owner_user_id`) plus every legal entity they hold an
    EFFECTIVE representation of — exactly the set `GET /auth/me` already reports
    back to the user as "who I can act for".

    The set-shaped companion of `has_effective_representation` above, and the
    reason it exists: a caller asking about ONE applicant can ask that one; a
    caller building a QUERY over somebody's own rows (`GET /api/v1/permits`,
    3.11a) cannot, and would otherwise have to import `auth.repo` across the
    module boundary. "Effective" means the same thing here as everywhere else in
    this file — `status='active'` and not past `valid_until`, judged against
    `business_today()`, never `date.today()` (lesson).

    No permission and no zone rule, like `get_applicant`/`role_code` above: the
    caller is another SERVICE inside this process, and the gates live on the
    routes that reach it.
    """
    own = await repo.get_own_applicant(db, user_id)
    ids = [] if own is None else [own.id]
    ids.extend(
        applicant.id
        for _, applicant in await repo.effective_representations(db, user_id, business_today())
    )
    return ids


async def update_applicant_address(
    db: AsyncSession, applicant_id: uuid.UUID, *, address: str, actor: User
) -> Applicant:
    """`PATCH /auth/applicants/{applicant_id}/address` (ruling #113): the
    dequeuing route — whatever gate `applications.checks.missing_for_pricing`
    puts on a missing address at SUBMISSION, this is where a citizen fills
    it in, at any time, DRAFT or not (registration itself stays free of it —
    the gate is at submission, not here).

    404 `ERR-SYS-003` for an `applicant_id` this caller has no claim on, and
    the SAME 404 for one that plain does not exist — `own_applicant_ids`
    answers both at once, because an id it does not name is either a
    stranger's or nobody's, and a 403 here would make this route an
    applicant-existence oracle for anybody holding a session (the identical
    reasoning `applications.service._readable_application` already states in
    full).

    "No claim" means exactly what `own_applicant_ids` means everywhere else
    in this module: the caller's own individual row, or a legal entity they
    hold an EFFECTIVE representation of — `status='active'` and not past
    `valid_until`, judged against `business_today()` inside that function,
    never `date.today()` (lesson). One definition, reused rather than
    re-derived: a representative who may act for a legal applicant here is
    exactly the same set that may file for it.
    """
    if applicant_id not in await own_applicant_ids(db, actor.id):
        raise err("ERR-SYS-003", details={"applicant": str(applicant_id)})
    applicant = await db.get(Applicant, applicant_id)
    assert applicant is not None  # own_applicant_ids only ever names rows that exist
    applicant.address = address
    await audit.log(
        db,
        action="applicant.update_address",
        user_id=actor.id,
        object_type="applicant",
        object_id=applicant.id,
    )
    return applicant


async def update_contact(
    db: AsyncSession,
    user: User,
    *,
    phone: str | None,
    email: str | None,
    otp_token: str | None,
    ip: str | None,
) -> None:
    """Change one's own phone or e-mail.

    **A staff member changes their phone with no code at all** (decision #150):
    nothing is ever sent to them over SMS, so a confirmation they cannot receive
    would only be a number they cannot change. `phone_verified_at` is CLEARED
    rather than stamped in that case — the number is what the employee typed, and
    recording it as verified would be a claim nobody checked. An applicant still
    verifies: their phone is the channel the permit actually travels on.

    E-mail is unchanged for everyone. Its code arrives by e-mail, costs nothing to
    send, and is the only proof the address exists.
    """
    now = datetime.now(UTC)
    own = await repo.get_own_applicant(db, user.id)
    is_applicant = await repo.role_code(db, user) == APPLICANT_ROLE_CODE
    if phone is not None:
        if is_applicant:
            if otp_token is None:
                raise err("ERR-VAL-001", details={"field": "otp_token"})
            await consume_otp_token(db, token=otp_token, purpose="phone_verify", target=phone)
            user.phone_verified_at = now
        else:
            user.phone_verified_at = None
        user.phone = phone
        if own is not None:
            own.phone = phone
    else:
        assert email is not None  # schema guarantees exactly one
        if otp_token is None:
            raise err("ERR-VAL-001", details={"field": "otp_token"})
        await consume_otp_token(db, token=otp_token, purpose="email_verify", target=email)
        user.email = email
        user.email_verified_at = now
        if own is not None:
            own.email = email
    await audit.log(
        db,
        action="user.update_contact",
        user_id=user.id,
        # `verified` says whether a code was actually checked: since #150 a staff
        # phone change is recorded as an unverified one, and the journal is the
        # only place that distinction survives.
        extra={
            "field": "phone" if phone is not None else "email",
            "verified": user.phone_verified_at is not None if phone is not None else True,
        },
        ip=ip,
    )


@dataclass(frozen=True)
class NotificationContact:
    """What `notifications` (level 2) needs to know about a recipient. Returning a
    plain value object keeps the module boundary intact: no User instance and no
    auth table leaves this service (design/01 rule 2)."""

    user_id: uuid.UUID
    full_name: str
    language: str
    status: str
    phone: str | None
    phone_verified: bool
    email: str | None
    email_verified: bool
    # Decision #150: SMS is for people who are NOT in the system. A staff member
    # reads the same notification in the cabinet they already have open, so the
    # `sms` channel is closed to every role but `applicant` — and this flag is what
    # `notifications` decides that on, since roles live in this module.
    is_applicant: bool


async def get_notification_contact(
    db: AsyncSession, user_id: uuid.UUID
) -> NotificationContact | None:
    user = await db.get(User, user_id)
    if user is None:
        return None
    return NotificationContact(
        user_id=user.id,
        full_name=user.full_name,
        language=user.language,
        status=user.status,
        phone=user.phone,
        phone_verified=user.phone_verified_at is not None,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        is_applicant=await repo.role_code(db, user) == APPLICANT_ROLE_CODE,
    )


async def get_applicant(db: AsyncSession, applicant_id: uuid.UUID) -> Applicant | None:
    """One `applicants` row by id, or None. No permission and no zone rule — the
    same shape as `get_notification_contact` above: the caller is another SERVICE
    inside this process, and the gates live on the routes that reach it.

    `applicants` lives in this module, so a level-3/4 caller holding only an
    `applicant_id` (permits 3.11a copies the holder's name and PINFL/STIR into the
    immutable permit snapshot; `applications` carries the id and nothing more) has
    no other lawful way to read it — cross-module calls go through the other
    module's service, never its repo (CLAUDE.md module boundary)."""
    return await db.get(Applicant, applicant_id)


async def get_oneid_snapshot_by_pinfl(db: AsyncSession, pinfl: str) -> dict[str, Any] | None:
    """Read-only seam, the same shape as `get_notification_contact`/
    `get_applicant` above: another module's SERVICE is the only lawful
    caller (CLAUDE.md module boundary — no `auth.repo` import from outside).

    First caller: `beekeepers.service.lookup_by_pinfl` (ruling #182's
    "honest auto-fill" — a beekeeper register form filled from whatever a
    citizen's own OneID login already told us, never invented). Returns the
    RAW snapshot dict (`OneIdProfile.to_snapshot()`'s own shape, stage 3.4/
    5.1) or None when nobody with this PINFL has ever signed in through
    OneID: `login_or_create_by_pinfl` is the only writer of `users.
    oneid_profile`, and only an OneID login passes a `snapshot` at all — a
    user who only ever used E-IMZO, or does not exist, reads the same as
    each other here, and the caller cannot and need not tell them apart."""
    user = await repo.get_user_by_pinfl(db, pinfl)
    if user is None:
        return None
    return user.oneid_profile


async def role_code(db: AsyncSession, user: User) -> str | None:
    """This user's `roles.code`, or None if the role row vanished (should not
    happen: FK). No permission and no zone rule — the same shape as
    `get_applicant` above: the caller is another SERVICE inside this process.

    `permits` (3.11a) needs it because a permit's four signature lines are
    role-based (`permits/signers.py`, ruling 4), and it reads the fact through
    this service because that is the boundary rule's default (CLAUDE.md:
    cross-module calls go through the other module's service). It is not the only
    way: `signatures.service`'s own module docstring documents a NARROW exception
    for `auth.repo.role_code`/`permission_codes` read directly, and
    `norms.service`, `gis.service`, `admin.users_service` and
    `permits.service._holds_view_any` all use it for an in-handler permission
    check. This wrapper is the plain read; reach past it only for that shape, and
    say so where you do. `auth.deps._authorize` reads the same fact through
    `repo.role_code` directly, being inside this module.
    """
    return await repo.role_code(db, user)


async def role_of(db: AsyncSession, user: User) -> Role | None:
    """This user's whole `roles` row, or None if it vanished (should not
    happen: FK). `role_code` above's sibling, and the same shape: no permission
    and no zone rule, because the caller is another SERVICE in this process.

    `applications` (3.9a) needs the ROW rather than the code because decision
    #29's approval ceilings — `max_approve_amount` and `max_approve_area` — are
    columns of `roles`, and reading them through `auth.repo` from another module
    would reach past this module's declared surface for two attributes.
    Comparing them against an application's amount and area is the CALLER's
    business rule, not this module's, so what comes back is the row and not a
    verdict.
    """
    return await repo.get_role(db, user.role_id)


async def list_user_ids_by_role_codes(
    db: AsyncSession, role_codes: Sequence[str]
) -> list[uuid.UUID]:
    """Active users holding one of these roles — the audience of system alerts."""
    rows = (
        await db.execute(
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .where(Role.code.in_(role_codes), User.status == "active")
        )
    ).scalars()
    return list(rows)


async def user_ids_with_permission(
    db: AsyncSession, permission_code: str, *, organization_id: uuid.UUID
) -> list[uuid.UUID]:
    """Active users of `organization_id` holding `permission_code`, by role or
    by a personal grant — the org-wide counterpart of `permission_codes`
    above, which answers the same question for one user already in hand. A
    generic, module-agnostic lookup: `applications`' auto-assignment (3.9b
    task 1) is the first caller, asking about its own `applications.review`."""
    return await repo.user_ids_with_permission(db, permission_code, organization_id=organization_id)


async def set_language(db: AsyncSession, user: User, language: str, *, ip: str | None) -> None:
    """Notification language (ruling 17). Not routed through the OTP-guarded contact
    change: switching UI language is not a contact change."""
    if language not in LOCALES:
        raise err("ERR-VAL-001", details={"field": "language"})
    user.language = language
    await audit.log(
        db,
        action="user.set_language",
        user_id=user.id,
        object_type="user",
        object_id=user.id,
        ip=ip,
        extra={"language": language},
    )


# --- Stage 13 (ruling #204): batch name readers for the register exports ------
# Other modules' `export.py` resolve the ids their rows carry into names
# through these — one query per table, never one per row, and always via
# this service (cross-module calls go through `service`, never `repo`).


async def applicant_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    return await repo.applicant_names(db, ids)


async def user_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    return await repo.user_full_names(db, ids)
