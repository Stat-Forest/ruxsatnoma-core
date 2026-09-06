"""Fixtures for `archive`. Reuses `search`'s own `make_application` (a plain
importable, not a fixture — no reason for two functions building the same
shape of row) and the same gis/permits primitives every other module's test
package already shares."""

import pytest

from app.config import get_settings
from tests.modules.gis.conftest import _client_for as _client_for
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import make_contour as make_contour
from tests.modules.gis.conftest import make_version as make_version
from tests.modules.gis.conftest import other_leshoz as other_leshoz
from tests.modules.gis.conftest import random_box_wkt as random_box_wkt
from tests.modules.permits.conftest import grazing_activity_id as grazing_activity_id
from tests.modules.permits.conftest import make_permit_on_contour as make_permit_on_contour
from tests.modules.search.conftest import _client_with_role as _client_with_role
from tests.modules.search.conftest import make_application as make_application


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    """The app under test must open the TEST database (lesson: an autouse
    fixture applies only within its own package)."""
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
