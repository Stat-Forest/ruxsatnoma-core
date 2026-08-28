"""admin API tests drive a real app (create_app + lifespan) against the test DB,
and need a signed-in user: the /refs endpoints are authenticated (ruling 10)."""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.modules.admin.models import Organization


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def agency(db) -> Organization:
    """The one root organization every hierarchy test hangs off.

    Ruling 6 allows exactly one `agency` row, and API tests commit theirs, so this is
    a committed get-or-create shared by the whole admin suite — never a fresh row per
    test (the partial unique index would reject the second one).
    """
    existing = (
        await db.execute(select(Organization).where(Organization.kind == "agency"))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    org = Organization(
        kind="agency",
        code="agency",
        name={"uz_cyrl": "Ўрмон хўжалиги агентлиги", "en": "Forestry Agency"},
    )
    db.add(org)
    await db.commit()
    return org
