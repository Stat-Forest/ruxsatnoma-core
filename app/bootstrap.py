"""Bootstrap the first sys_admin from the CLI (ruling 13). No open staff registration.

Usage: uv run python -m app.bootstrap --login admin --full-name "Admin" [--password ...]
Prints the one-time password (if generated) and the otpauth:// URI once; store them now.
--password is for automation; prefer the generated one-time password.
"""

import argparse
import asyncio
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Imports every modules/*/models.py so Base.metadata knows the whole schema before
# the first flush. Importing only auth.models left `users.district_id`'s FK
# unresolvable (`districts` lives in admin.models, never imported), so SQLAlchemy
# raised `NoReferencedTableError` at insert time and a fresh deployment could not
# create its first sys_admin at all (decision #61).
import app.models_registry  # noqa: F401
from app.config import get_settings
from app.core.crypto import encrypt_str
from app.core.security import (
    hash_password,
    new_totp_secret,
    totp_provisioning_uri,
    validate_password_policy,
)
from app.db import make_engine, make_session_factory
from app.modules.audit import service as audit
from app.modules.auth.models import Role, User


@dataclass
class BootstrapResult:
    one_time_password: str
    otpauth_uri: str


async def bootstrap_admin(
    db: AsyncSession, *, login: str, full_name: str, password: str | None = None
) -> BootstrapResult | None:
    """Create a sys_admin; returns None (no changes) if the login already exists."""
    existing = (await db.execute(select(User).where(User.login == login))).scalar_one_or_none()
    if existing is not None:
        return None
    role_id = (await db.execute(select(Role.id).where(Role.code == "sys_admin"))).scalar_one()
    if password is not None:
        validate_password_policy(password)
    one_time = password or ("Aa1!" + secrets.token_urlsafe(12))
    secret = new_totp_secret()
    user = User(
        login=login,
        full_name=full_name,
        role_id=role_id,
        password_hash=hash_password(one_time),
        mfa_secret=encrypt_str(secret),
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    # audit invariant (decision #38): user_id=None — the actor is the CLI operator
    await audit.log(
        db,
        action="user.create",
        object_type="user",
        object_id=user.id,
        basis="bootstrap CLI",
        extra={"login": login},
    )
    return BootstrapResult(
        one_time_password=one_time, otpauth_uri=totp_provisioning_uri(secret, login)
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Create the first sys_admin")
    parser.add_argument("--login", required=True)
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()
    engine = make_engine(get_settings().database_url)
    try:
        async with make_session_factory(engine)() as db:
            result = await bootstrap_admin(
                db, login=args.login, full_name=args.full_name, password=args.password
            )
            await db.commit()
    finally:
        await engine.dispose()
    if result is None:
        print(f"user '{args.login}' already exists — nothing done")
    else:
        print(f"one-time password: {result.one_time_password}")
        print(f"TOTP enrollment:   {result.otpauth_uri}")


if __name__ == "__main__":
    asyncio.run(_main())
