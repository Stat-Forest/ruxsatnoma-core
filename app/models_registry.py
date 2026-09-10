"""Imports every modules/*/models.py so Base.metadata sees all tables.

Imported by migrations/env.py and tests/conftest.py. A model file missing
here is caught by test_autogenerate_diff_empty: its table exists in the DB
but not in metadata, producing a drop diff.
"""

import app.core.models  # noqa: F401
import app.modules.admin.models  # noqa: F401
import app.modules.audit.models  # noqa: F401
import app.modules.auth.models  # noqa: F401
import app.modules.integrations.models  # noqa: F401
from app.modules.applications import models as applications_models  # noqa: F401
from app.modules.archive import models as archive_models  # noqa: F401
from app.modules.beekeepers import models as beekeepers_models  # noqa: F401
from app.modules.gis import models as gis_models  # noqa: F401
from app.modules.help import models as help_models  # noqa: F401
from app.modules.inspections import models as inspections_models  # noqa: F401
from app.modules.norms import models as norms_models  # noqa: F401
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.oversight import models as oversight_models  # noqa: F401
from app.modules.payments import models as payments_models  # noqa: F401
from app.modules.permits import models as permits_models  # noqa: F401
from app.modules.public import models as public_models  # noqa: F401
from app.modules.reports import models as reports_models  # noqa: F401
from app.modules.search import models as search_models  # noqa: F401
from app.modules.signatures import models as signatures_models  # noqa: F401
