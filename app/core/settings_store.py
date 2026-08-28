"""Runtime policy parameters: code defaults, DB overrides, short process cache.

Level 0 by design (ruling 8): `auth` (level 1) reads session and lockout policy from
here; `admin` (also level 1) writes it through its own service. Putting the reader in
core avoids an auth → admin dependency, which would close a cycle (design/01 rule 3).

The DB stores only OVERRIDES. A missing row means "use the default below", so a fresh
database is fully functional and `system_settings` never needs seeding.

The cache is per-process and expires after 60 seconds: with several uvicorn workers an
admin's change reaches every worker within that window, and the hot path (every
authenticated request reads `session_idle_minutes`) does not hit the DB each time.
"""

import time
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import err
from app.core.models import SystemSetting

CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: type
    default: Any
    description: str


SETTING_SPECS: dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec("session_absolute_hours", int, 12, "Session lifetime in hours"),
        SettingSpec("session_idle_minutes", int, 30, "Sign-out after this much inactivity"),
        SettingSpec("login_max_attempts", int, 5, "Failed logins before the account locks"),
        SettingSpec("login_lockout_minutes", int, 15, "How long a locked account stays locked"),
        SettingSpec("mfa_token_ttl_minutes", int, 5, "Lifetime of the interim MFA token"),
        SettingSpec("mfa_max_attempts", int, 5, "Wrong TOTP codes before the MFA token burns"),
    )
}

_cache: dict[str, tuple[float, Any]] = {}


def invalidate(key: str | None = None) -> None:
    """Drop one key (after an admin update) or the whole cache (tests, bootstrap)."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def coerce(spec: SettingSpec, raw: Any) -> Any:
    """Validate an incoming value against the spec; raises ERR-VAL-001 on bad input.

    JSON bodies may carry "45" where 45 is meant, so numeric strings are accepted.
    Policy values are positive integers — 0 would mean "lock out forever" or
    "session already expired", which no admin means to type.
    """
    if spec.type is int:
        if isinstance(raw, bool):  # bool is an int subclass — not a policy value
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected integer"})
        try:
            value = int(raw)
        # Deliberately parenthesized, not the PEP 758 bare form: for years
        # `except Foo, bar:` meant Python 2's except-as binding, so the bare
        # shape reads like that trap. ruff format's py314 target rewrites it
        # away otherwise, hence the fmt:skip.
        except (TypeError, ValueError):  # fmt: skip
            raise err(
                "ERR-VAL-001", details={"setting": spec.key, "reason": "expected integer"}
            ) from None
        if value <= 0:
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "must be positive"})
        return value
    if spec.type is bool:
        if not isinstance(raw, bool):
            raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected boolean"})
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise err("ERR-VAL-001", details={"setting": spec.key, "reason": "expected text"})
    return raw


async def get_setting(db: AsyncSession, key: str) -> Any:
    """Effective value: DB override if present and well-formed, else the code default."""
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise KeyError(f"unknown setting: {key!r}")  # programming error, not user input
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    row = await db.get(SystemSetting, key)
    value = spec.default
    if row is not None:
        try:
            value = coerce(spec, row.value)
        except Exception:
            # A malformed row (hand-edited SQL) must not break every request.
            structlog.get_logger().error(
                "system_setting_invalid", key=key, value=row.value, using_default=spec.default
            )
    _cache[key] = (now + CACHE_TTL_SECONDS, value)
    return value


async def get_int(db: AsyncSession, key: str) -> int:
    value = await get_setting(db, key)
    assert isinstance(value, int)  # SETTING_SPECS guarantees the type
    return value
