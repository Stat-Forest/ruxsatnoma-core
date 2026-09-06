"""Fixtures for `dashboard`. An autouse `_app_on_test_db` guard is required in
EVERY HTTP-tested package (lesson) — it does not propagate from a sibling
package's conftest, so `tests/modules/oversight/conftest.py`'s copy does not
cover this directory."""

import pytest

from app.config import get_settings
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
