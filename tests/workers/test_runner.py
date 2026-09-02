"""`app.workers.runner.main` — the `python -m app.workers` entry point for a
standalone (`workers_mode=off`) deployment."""

import subprocess
import sys
from pathlib import Path


def test_a_standalone_worker_process_registers_event_subscriptions_before_running():
    """Review round 1, finding I2: `main()` never calls `app.main.create_app()`,
    so nothing else wires up the bus's subscribers in a `workers_mode=off`
    deployment — a handler a future stage adds would silently never fire for an
    event this process publishes. A fresh interpreter that imports only
    `app.workers.runner`, exactly what the real process does, is the only way to
    prove `main()` itself reaches `register_event_subscriptions()`, the same
    reason `test_a_standalone_worker_process_registers_the_notification_senders_
    on_import` (tests/workers/test_outbox_worker.py) uses a subprocess rather
    than an in-process check — the rest of the test suite already imports
    app.main by the time this test would otherwise run, which would hide a
    missing call here.

    The stub raises SystemExit the moment `main()` calls it, so the process
    exits cleanly before ever starting the outbox loop or the scheduler —
    `main()` itself blocks on an OS signal and must never be run to completion
    in a test.
    """
    backend_root = Path(__file__).resolve().parents[2]
    script = (
        "import app.workers.runner as runner\n"
        "\n"
        "def _stub() -> None:\n"
        "    raise SystemExit(0)\n"
        "\n"
        "runner.register_event_subscriptions = _stub\n"
        "runner.main()\n"
        "raise SystemExit('main() returned instead of calling the stub')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
