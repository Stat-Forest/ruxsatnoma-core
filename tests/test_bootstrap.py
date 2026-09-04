"""The first sys_admin must be creatable on a database that has only migrations applied.

Regression: bootstrap imported auth's models alone, so users.district_id -> districts had no
target in Base.metadata and the flush raised NoReferencedTableError. A fresh deployment
could not create any user at all.
"""

import subprocess
import sys
from pathlib import Path

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


def test_a_fresh_process_that_imports_only_bootstrap_can_create_the_first_sys_admin():
    """The regression this whole file is named after cannot be seen from an in-process
    test: tests/conftest.py imports app.models_registry at module scope (`# populate
    Base.metadata for migration tests`), so by the time any test in this suite calls
    bootstrap_admin, Base.metadata already has every table — including districts —
    regardless of what app/bootstrap.py itself imports. The two tests above pin
    bootstrap_admin's behaviour but PASS even with the `import app.models_registry`
    line removed from app/bootstrap.py; they cannot fail on this bug.

    Only a fresh interpreter that imports app.bootstrap and nothing else — exactly
    what `python -m app.bootstrap`, the real deployment entry point, does — can prove
    it. Same masking problem, same cure as
    tests/workers/test_runner.py::test_a_standalone_worker_process_registers_event_
    subscriptions_before_running and
    tests/workers/test_outbox_worker.py::test_a_standalone_worker_process_registers_
    the_notification_senders_on_import. Remove the `import app.models_registry` line
    in app/bootstrap.py to watch this fail: the subprocess raises
    sqlalchemy.exc.NoReferencedTableError naming 'districts'.
    """
    backend_root = Path(__file__).resolve().parents[1]
    script = (
        "import asyncio\n"
        "import uuid\n"
        "\n"
        "from app.bootstrap import bootstrap_admin\n"
        "from app.config import get_settings\n"
        "from app.db import make_engine, make_session_factory\n"
        "\n"
        "async def main() -> None:\n"
        "    engine = make_engine(get_settings().database_url_test)\n"
        "    try:\n"
        "        async with make_session_factory(engine)() as db:\n"
        "            login = f'subprocess-bootstrap-{uuid.uuid4().hex[:12]}'\n"
        "            result = await bootstrap_admin(\n"
        "                db, login=login, full_name='Subprocess Bootstrap Check'\n"
        "            )\n"
        "            assert result is not None, 'bootstrap_admin returned None for a new login'\n"
        "            await db.rollback()  # this process must leave no row behind\n"
        "    finally:\n"
        "        await engine.dispose()\n"
        "\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
