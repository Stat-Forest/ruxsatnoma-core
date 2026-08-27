"""Imports every modules/*/models.py so Base.metadata sees all tables.

Imported by migrations/env.py and tests/conftest.py. A model file missing
here is caught by test_autogenerate_diff_empty: its table exists in the DB
but not in metadata, producing a drop diff.
"""

import app.modules.audit.models  # noqa: F401
