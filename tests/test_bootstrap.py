"""The first sys_admin must be creatable on a database that has only migrations applied.

Regression: bootstrap imported auth's models alone, so users.district_id -> districts had no
target in Base.metadata and the flush raised NoReferencedTableError. A fresh deployment
could not create any user at all.
"""

from app.bootstrap import bootstrap_admin


async def test_bootstrap_creates_the_first_sys_admin(db):
    result = await bootstrap_admin(db, login="deploy-admin", full_name="Deploy Admin")

    assert result is not None
    assert result.one_time_password
    assert result.otpauth_uri.startswith("otpauth://totp/")


async def test_bootstrap_is_idempotent_on_an_existing_login(db):
    await bootstrap_admin(db, login="deploy-admin", full_name="Deploy Admin")

    again = await bootstrap_admin(db, login="deploy-admin", full_name="Deploy Admin")

    assert again is None
