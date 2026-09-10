"""beekeepers test plumbing.

Every test in this package builds its user, session and permission grants
inline and commits BEFORE opening a client (the shape `tests/modules/admin/
test_organizations_admin.py` uses) — no `_client_for`-style fixture chain
needed, since no test here mixes a `db`-writing fixture with a client
fixture in the same parameter list (lessons.md's own warning about fixture
instantiation order)."""

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """Same guard as every other HTTP-tested package: the app under test must
    open the TEST database, not the shared dev one."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
