"""Shared signatures fixtures. `a_certificate` is the minimal bound certificate that
every later task's own fixtures (a_signature, etc.) build on top of — later tasks add
those themselves rather than growing this file (pre-flight ruling P4).

`_app_on_test_db` is infrastructure, not a data fixture, so ruling P4 does not apply
to it — it is the same autouse guard every other HTTP-driven module's own conftest.py
carries (gis, admin, auth, norms, notifications, integrations, core): Task 7 is the
first in this module to drive requests through `create_app()`'s own lifespan, and
without this, the app under test opens `DATABASE_URL` (the shared dev database), not
this worktree's `DATABASE_URL_TEST` — every session cookie this file's fixtures write
would be invisible to it, and every request would 401."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import uuid7
from app.modules.signatures.models import Certificate


@pytest.fixture(autouse=True)
def _app_on_test_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", get_settings().database_url_test)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def a_certificate(db: AsyncSession) -> uuid.UUID:
    """An active certificate not bound to any particular user — the FK target
    `signatures.certificate_id` needs. `serial_number`/`issuer` are randomized so
    repeated fixture use never collides with `uq_certificate_identity`."""
    now = datetime.now(UTC)
    cert = Certificate(
        id=uuid7(),
        user_id=None,
        serial_number=f"SER-{uuid.uuid4().hex[:12]}",
        issuer=f"ISS-{uuid.uuid4().hex[:8]}",
        subject="CN=Test Signer",
        pinfl_or_stir="12345678901",
        valid_from=now,
        valid_to=now + timedelta(days=365),
        status="active",
    )
    db.add(cert)
    await db.flush()
    return cert.id
