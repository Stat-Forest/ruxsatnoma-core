"""Shared signatures fixtures. `a_certificate` is the minimal bound certificate that
every later task's own fixtures (a_signature, etc.) build on top of — later tasks add
those themselves rather than growing this file (pre-flight ruling P4)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import uuid7
from app.modules.signatures.models import Certificate


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
