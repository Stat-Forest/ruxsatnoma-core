"""help API tests drive a real app (create_app + lifespan) against the test DB."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.modules.auth.models import UserPermission
from app.modules.help.permissions import FAQ_MANAGE, TICKETS_MANAGE
from tests.modules.admin.test_organizations_admin import auth_client
from tests.modules.auth.test_sessions import make_session, make_user


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def faq_manager(db: AsyncSession):
    user = await make_user(db, role_code="executor_staff")
    db.add(UserPermission(user_id=user.id, permission_code=FAQ_MANAGE))
    await db.flush()
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return user, token, csrf


async def tickets_manager(db: AsyncSession):
    user = await make_user(db, role_code="executor_staff")
    db.add(UserPermission(user_id=user.id, permission_code=TICKETS_MANAGE))
    await db.flush()
    _, token, csrf = await make_session(db, user)
    await db.commit()
    return user, token, csrf


__all__ = ["auth_client", "faq_manager", "tickets_manager"]
