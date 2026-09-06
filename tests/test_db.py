import os

import pytest
from sqlalchemy import text

from app.config import get_settings


async def test_db_roundtrip(db):
    result = await db.execute(text("SELECT 1"))
    assert result.scalar_one() == 1


async def test_a_parallel_worker_talks_only_to_its_own_database(db):
    """`make test` runs `-n 4`, and the suite has no transactional rollback:
    two workers on one database wipe and re-create each other's tables (see
    `_use_a_database_of_this_workers_own` in conftest). This asserts the split
    actually happened rather than trusting that it did — a rename of
    PYTEST_XDIST_WORKER, or an env var set after the first get_settings(),
    would otherwise show up as unrelated flakes three modules away.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        pytest.skip("serial run — there is only one database to talk to")
    assert get_settings().database_url_test.endswith(f"_{worker}")
    current = (await db.execute(text("select current_database()"))).scalar_one()
    assert current.endswith(f"_{worker}")
