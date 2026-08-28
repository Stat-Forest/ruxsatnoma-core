"""Service layer for user administration (С23): CRUD, credentials handout,
block/unblock/delete. Reaches users/sessions/roles through `auth.repo`; password
and TOTP primitives stay in `app.core.security` — this module only orchestrates
them (design/01: `admin` may import `auth`, never the reverse).
"""

import random
import secrets
import string
import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.abac import Zone, zone_filter, zone_of
from app.core.crypto import encrypt_str
from app.core.errors import err
from app.core.schemas import Page, PageParams
from app.core.security import (
    hash_password,
    new_totp_secret,
    totp_provisioning_uri,
    validate_password_policy,
)
from app.modules.admin.users_schemas import (
    UserAdminOut,
    UserCreatedOut,
    UserCreateIn,
    UserFilters,
    UserPatchIn,
)
from app.modules.audit import service as audit
from app.modules.auth import repo as auth_repo
from app.modules.auth.deps import SUPERUSER_ROLE
from app.modules.auth.models import Role, User
from app.modules.auth.permissions import USERS_MANAGE

# One-time password generator: 12 chars from a de-ambiguated alphabet (no l/I/O/0/1
# — a person reads these off a screen to type them once). A pure alnum draw could
# never satisfy `validate_password_policy` (it requires a special character, and
# none of the alphabet below is one), so one char from each required class is
# guaranteed by construction, then the rest is filled and the whole thing shuffled.
_AMBIGUOUS = "lIO01"
_OTP_UPPER = "".join(c for c in string.ascii_uppercase if c not in _AMBIGUOUS)
_OTP_LOWER = "".join(c for c in string.ascii_lowercase if c not in _AMBIGUOUS)
_OTP_DIGITS = "".join(c for c in string.digits if c not in _AMBIGUOUS)
_OTP_SPECIAL = "!@#$%*-_="
_OTP_ALPHABET = _OTP_UPPER + _OTP_LOWER + _OTP_DIGITS
_OTP_LENGTH = 12


def _generate_one_time_password() -> str:
    required = [
        secrets.choice(_OTP_UPPER),
        secrets.choice(_OTP_LOWER),
        secrets.choice(_OTP_DIGITS),
        secrets.choice(_OTP_SPECIAL),
    ]
    rest = [secrets.choice(_OTP_ALPHABET) for _ in range(_OTP_LENGTH - len(required))]
    chars = required + rest
    random.SystemRandom().shuffle(chars)
    return "".join(chars)


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


_CREATE_SNAPSHOT_FIELDS = (
    "login",
    "full_name",
    "pinfl",
    "position",
    "organization_id",
    "region_id",
    "district_id",
    "phone",
    "email",
    "status",
    "must_change_password",
    "valid_until",
)


def _user_snapshot(user: User, role_code: str) -> dict[str, Any]:
    """Full JSON-safe snapshot for `user.create`'s audit `new_value` — never
    includes `password_hash`/`mfa_secret` (they are not in the field list)."""
    data = {field: _json_safe(getattr(user, field)) for field in _CREATE_SNAPSHOT_FIELDS}
    data["role_id"] = str(user.role_id)
    data["role_code"] = role_code
    return data


def _user_field_snapshot(user: User, role_code: str, fields: set[str]) -> dict[str, Any]:
    """JSON-safe values for exactly `fields` (the keys present in an incoming
    patch) — `user.update` audits are scoped to what the caller touched, unlike
    `user.create`'s full snapshot."""
    data: dict[str, Any] = {}
    for field in fields:
        data[field] = role_code if field == "role_code" else _json_safe(getattr(user, field))
    return data


def _to_admin_out(user: User, role_code: str) -> UserAdminOut:
    return UserAdminOut(
        id=user.id,
        login=user.login,
        full_name=user.full_name,
        pinfl=user.pinfl,
        position=user.position,
        role_id=user.role_id,
        role_code=role_code,
        organization_id=user.organization_id,
        region_id=user.region_id,
        district_id=user.district_id,
        phone=user.phone,
        email=user.email,
        status=user.status,
        must_change_password=user.must_change_password,
        valid_until=user.valid_until,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


async def _user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await auth_repo.get_user(db, user_id)
    if user is None:
        raise err("ERR-SYS-003", details={"user": str(user_id)})
    return user


def _guard_not_self(user_id: uuid.UUID, actor: User) -> None:
    """Ruling 10: an admin cannot block or delete their own account."""
    if user_id == actor.id:
        raise err("ERR-VAL-001", details={"reason": "own_account"})


async def _may_manage(db: AsyncSession, actor: User) -> bool:
    if await auth_repo.role_code(db, actor) == SUPERUSER_ROLE:
        return True
    return USERS_MANAGE in await auth_repo.permission_codes(db, actor)


def _within_zone(zone: Zone, user: User) -> bool:
    """Per-row equivalent of `zone_filter`'s SQL (design/01 'own zone' mechanics),
    for a single already-fetched row rather than a query — same semantics kept in
    sync deliberately, not a second SQL round-trip: an axis whose `zone` field is
    None is unrestricted; a set axis requires an exact match on `user`'s column.
    An all-None `zone` (republic-wide) therefore matches every user, same as
    `zone_filter`'s `true()` fallback."""
    if zone.region_id is not None and zone.region_id != user.region_id:
        return False
    if zone.district_id is not None and zone.district_id != user.district_id:
        return False
    if zone.organization_id is not None and zone.organization_id != user.organization_id:
        return False
    return True


async def _staff_role_or_422(db: AsyncSession, role_code: str) -> Role:
    """A role usable for `POST /admin/users` / role reassignment via PATCH: must
    exist, be active, and not be `applicant` (applicants are born via OneID/E-IMZO,
    never created or reassigned by hand here — ruling 8/9)."""
    role = await auth_repo.get_role_by_code(db, role_code)
    if role is None:
        raise err("ERR-SYS-003", details={"role_code": role_code})
    if role.status != "active":
        raise err("ERR-VAL-001", details={"role_code": role_code, "reason": "role_archived"})
    if role.code == "applicant":
        raise err("ERR-VAL-001", details={"reason": "staff_roles_only"})
    return role


async def _check_pinfl_available(
    db: AsyncSession, pinfl: str | None, *, exclude_user_id: uuid.UUID | None = None
) -> None:
    if pinfl is None:
        return
    existing = await auth_repo.get_user_by_pinfl(db, pinfl)
    if existing is not None and existing.id != exclude_user_id:
        raise err(
            "ERR-VAL-001",
            details={"reason": "duplicate_pinfl", "existing_user_id": str(existing.id)},
        )


async def _check_login_available(
    db: AsyncSession, login: str | None, *, exclude_user_id: uuid.UUID | None = None
) -> None:
    if login is None:
        return
    existing = await auth_repo.get_user_by_login(db, login)
    if existing is not None and existing.id != exclude_user_id:
        raise err(
            "ERR-VAL-001",
            details={"reason": "duplicate_login", "existing_user_id": str(existing.id)},
        )


async def list_users(
    db: AsyncSession, *, params: PageParams, filters: UserFilters, actor: User
) -> Page[UserAdminOut]:
    """Ruling 7: a viewer without `auth.users.manage` (and not sys_admin) sees only
    their own zone (`zone_of(actor)` over `region_id`/`district_id`/`organization_id`);
    a manage-holder sees everything, filters notwithstanding."""
    zone_condition = None
    if not await _may_manage(db, actor):
        zone_condition = zone_filter(
            zone_of(actor),
            region_col=User.region_id,
            district_col=User.district_id,
            organization_col=User.organization_id,
        )
    rows, total = await auth_repo.list_users(
        db,
        role_code=filters.role_code,
        status=filters.status,
        organization_id=filters.organization_id,
        region_id=filters.region_id,
        q=filters.q,
        zone=zone_condition,
        offset=params.offset,
        limit=params.page_size,
    )
    items: list[UserAdminOut] = []
    for row in rows:
        role_code = await auth_repo.role_code(db, row)
        assert role_code is not None  # FK guarantees a role row
        items.append(_to_admin_out(row, role_code))
    return Page[UserAdminOut](
        items=items, total=total, page=params.page, page_size=params.page_size
    )


async def get_user(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> UserAdminOut:
    """Ruling 7 applies here too, not just to `list_users`: a viewer without
    `auth.users.manage` (and not sys_admin) may only read cards inside their own
    zone — a target outside it is `ERR-ACL-002`, not a 404 (the row exists, the
    actor just may not see it), matching the code the catalog already carries for
    zone violations."""
    user = await _user_or_404(db, user_id)
    if not await _may_manage(db, actor) and not _within_zone(zone_of(actor), user):
        raise err("ERR-ACL-002")
    role_code = await auth_repo.role_code(db, user)
    assert role_code is not None
    return _to_admin_out(user, role_code)


async def create_user(db: AsyncSession, *, data: UserCreateIn, actor: User) -> UserCreatedOut:
    role = await _staff_role_or_422(db, data.role_code)
    await _check_pinfl_available(db, data.pinfl)
    await _check_login_available(db, data.login)

    one_time_password = _generate_one_time_password()
    validate_password_policy(one_time_password)  # guaranteed by construction; safety net
    secret = new_totp_secret()
    user = User(
        login=data.login,
        full_name=data.full_name,
        role_id=role.id,
        pinfl=data.pinfl,
        position=data.position,
        organization_id=data.organization_id,
        region_id=data.region_id,
        district_id=data.district_id,
        phone=data.phone,
        email=data.email,
        valid_until=data.valid_until,
        # Same handout pattern as app/bootstrap.py's first sys_admin: hashed
        # one-time password, Fernet-encrypted TOTP secret, forced change on login.
        password_hash=hash_password(one_time_password),
        mfa_secret=encrypt_str(secret),
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    await audit.log(
        db,
        action="user.create",
        user_id=actor.id,
        object_type="user",
        object_id=user.id,
        new_value=_user_snapshot(user, role.code),
    )
    return UserCreatedOut(
        user=_to_admin_out(user, role.code),
        one_time_password=one_time_password,
        totp_uri=totp_provisioning_uri(secret, data.login),
    )


_PATCHABLE_SIMPLE_FIELDS = (
    "full_name",
    "position",
    "pinfl",
    "phone",
    "email",
    "organization_id",
    "region_id",
    "district_id",
    "valid_until",
)


async def patch_user(
    db: AsyncSession, *, user_id: uuid.UUID, data: UserPatchIn, actor: User
) -> UserAdminOut:
    user = await _user_or_404(db, user_id)
    fields = data.model_dump(exclude_unset=True)
    role_code_before = await auth_repo.role_code(db, user)
    assert role_code_before is not None  # FK guarantees a role row

    before = _user_field_snapshot(user, role_code_before, set(fields))

    await _check_pinfl_available(db, fields.get("pinfl"), exclude_user_id=user.id)
    await _check_login_available(db, fields.get("login"), exclude_user_id=user.id)

    new_role_code = role_code_before
    if "role_code" in fields:
        role = await _staff_role_or_422(db, fields["role_code"])
        new_role_code = role.code
        user.role_id = role.id

    # Ruling 9: assigning a staff role requires a login — either already stored,
    # or supplied in this same patch (staffifying an auto-created applicant).
    effective_login = fields["login"] if "login" in fields else user.login
    if new_role_code != "applicant" and not effective_login:
        raise err("ERR-VAL-001", details={"reason": "login_required"})

    for field in _PATCHABLE_SIMPLE_FIELDS:
        if field in fields:
            setattr(user, field, fields[field])
    if "login" in fields:
        user.login = fields["login"]

    await db.flush()
    after = _user_field_snapshot(user, new_role_code, set(fields))
    await audit.log(
        db,
        action="user.update",
        user_id=actor.id,
        object_type="user",
        object_id=user.id,
        old_value=before,
        new_value=after,
    )
    return _to_admin_out(user, new_role_code)


async def block_user(
    db: AsyncSession, *, user_id: uuid.UUID, reason: str, actor: User
) -> UserAdminOut:
    _guard_not_self(user_id, actor)
    user = await _user_or_404(db, user_id)
    before_status = user.status
    user.status = "blocked"
    await auth_repo.revoke_user_sessions(db, user.id)
    await db.flush()
    role_code = await auth_repo.role_code(db, user)
    assert role_code is not None
    await audit.log(
        db,
        action="user.block",
        user_id=actor.id,
        object_type="user",
        object_id=user.id,
        old_value={"status": before_status},
        new_value={"status": "blocked", "reason": reason},
    )
    return _to_admin_out(user, role_code)


async def unblock_user(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> UserAdminOut:
    user = await _user_or_404(db, user_id)
    before_status = user.status
    user.status = "active"
    user.failed_login_count = 0
    user.locked_until = None
    await db.flush()
    role_code = await auth_repo.role_code(db, user)
    assert role_code is not None
    await audit.log(
        db,
        action="user.unblock",
        user_id=actor.id,
        object_type="user",
        object_id=user.id,
        old_value={"status": before_status},
        new_value={"status": "active"},
    )
    return _to_admin_out(user, role_code)


async def delete_user(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> UserAdminOut:
    _guard_not_self(user_id, actor)
    user = await _user_or_404(db, user_id)
    before_status = user.status
    user.status = "deleted"
    await auth_repo.revoke_user_sessions(db, user.id)
    await db.flush()
    role_code = await auth_repo.role_code(db, user)
    assert role_code is not None
    await audit.log(
        db,
        action="user.delete",
        user_id=actor.id,
        object_type="user",
        object_id=user.id,
        old_value={"status": before_status},
        new_value={"status": "deleted"},
    )
    return _to_admin_out(user, role_code)


async def reset_password(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> str:
    user = await _user_or_404(db, user_id)
    one_time_password = _generate_one_time_password()
    validate_password_policy(one_time_password)
    user.password_hash = hash_password(one_time_password)
    user.must_change_password = True
    await auth_repo.revoke_user_sessions(db, user.id)
    await db.flush()
    await audit.log(
        db, action="user.reset_password", user_id=actor.id, object_type="user", object_id=user.id
    )
    return one_time_password


async def reset_mfa(db: AsyncSession, *, user_id: uuid.UUID, actor: User) -> str:
    user = await _user_or_404(db, user_id)
    secret = new_totp_secret()
    user.mfa_secret = encrypt_str(secret)
    await auth_repo.revoke_user_sessions(db, user.id)
    await db.flush()
    await audit.log(
        db, action="user.reset_mfa", user_id=actor.id, object_type="user", object_id=user.id
    )
    return totp_provisioning_uri(secret, user.login or str(user.id))
