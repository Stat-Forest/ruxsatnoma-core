"""Auth service: sessions, login+MFA, passwords. The only door for other modules."""

import asyncio
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
    Applicant,
    OtpCode,
    Representation,
    Role,
    Session,
    User,
    UserConsent,
)
from app.modules.integrations import service as integrations_service
from app.modules.integrations.adapters.eimzo import EimzoError, EimzoIdentity, get_eimzo_adapter
from app.modules.integrations.adapters.oneid import OneIdError, get_oneid_adapter

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
) -> tuple[User, Session, str, str]:
    """Shared core of OneID/E-IMZO logins (ruling 5): any-role entry by pinfl,
    auto-creating an applicant account on first contact."""
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
    row, token, csrf = await issue_session(db, user, ip=ip, user_agent=user_agent)
    await audit.log(
        db,
        action="user.login",
        user_id=user.id,
        extra={"method": method},
        ip=ip,
        user_agent=user_agent,
    )
    return user, row, token, csrf


async def login_via_oneid(
    db: AsyncSession, *, code: str, ip: str | None, user_agent: str | None
) -> tuple[User, Session, str, str]:
    adapter = get_oneid_adapter()
    try:
        profile = await adapter.exchange_code(code)
    except OneIdError as exc:
        raise err(exc.err_code) from exc
    return await login_or_create_by_pinfl(
        db,
        pinfl=profile.pinfl,
        full_name=profile.full_name,
        method="oneid",
        snapshot=profile.to_snapshot(),
        phone=profile.phone,
        ip=ip,
        user_agent=user_agent,
    )


EIMZO_CHALLENGE_TTL_MINUTES = 5


async def issue_eimzo_challenge(db: AsyncSession) -> str:
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
        identity = await adapter.verify_signed_challenge(signed_challenge)
    except EimzoError as exc:
        raise err(exc.err_code) from exc
    row = await repo.get_valid_otp(db, hash_token(identity.challenge), purpose="eimzo_challenge")
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
    if await repo.role_code(db, user) != "applicant":
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
    db: AsyncSession, *, signed_challenge: str, stir: str, signer_pinfl: str
) -> EimzoIdentity:
    """org_eri basis: a fresh org-cert signature naming this stir and this signer."""
    adapter = get_eimzo_adapter()
    try:
        identity = await adapter.verify_signed_challenge(signed_challenge)
    except EimzoError as exc:
        raise err("ERR-ACL-001", details={"basis": "org_eri", "reason": "bad signature"}) from exc
    row = await repo.get_valid_otp(db, hash_token(identity.challenge), purpose="eimzo_challenge")
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
    if await repo.role_code(db, user) != "applicant":
        raise err("ERR-ACL-001", details={"reason": "not an applicant account"})
    assert user.pinfl is not None
    legal_name = name
    requisites: dict[str, Any] | None = None
    if basis == "org_eri":
        assert signed_challenge is not None  # schema guarantees
        identity = await _verify_org_challenge(
            db, signed_challenge=signed_challenge, stir=stir, signer_pinfl=user.pinfl
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
    if await repo.role_code(db, candidate) != "applicant":
        raise err("ERR-ACL-001", details={"reason": "candidate is not an applicant account"})
    assert applicant.stir is not None and user.pinfl is not None
    if basis == "org_eri":
        assert signed_challenge is not None
        await _verify_org_challenge(
            db, signed_challenge=signed_challenge, stir=applicant.stir, signer_pinfl=user.pinfl
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


async def update_contact(
    db: AsyncSession,
    user: User,
    *,
    phone: str | None,
    email: str | None,
    otp_token: str,
    ip: str | None,
) -> None:
    now = datetime.now(UTC)
    own = await repo.get_own_applicant(db, user.id)
    if phone is not None:
        await consume_otp_token(db, token=otp_token, purpose="phone_verify", target=phone)
        user.phone = phone
        user.phone_verified_at = now
        if own is not None:
            own.phone = phone
    else:
        assert email is not None  # schema guarantees exactly one
        await consume_otp_token(db, token=otp_token, purpose="email_verify", target=email)
        user.email = email
        user.email_verified_at = now
        if own is not None:
            own.email = email
    await audit.log(
        db,
        action="user.update_contact",
        user_id=user.id,
        extra={"field": "phone" if phone is not None else "email"},
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
    )


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
