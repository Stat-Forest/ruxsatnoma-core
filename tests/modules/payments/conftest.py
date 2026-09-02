"""Fixtures for the payments module.

`applications` has no HTTP surface yet on `dev` (3.9a branch 1's own conftest note),
so an application row is built directly through the ORM — the same idiom
`tests/modules/applications/test_public_surface.py`'s `_draft` helper uses, with
`status` forced to APPROVED: payments only needs the row SHAPE an approved
application has, not a real transition (history row, audit entry) to reach it —
that stays branch 2's job.

`applicant` is re-exported from `tests.modules.applications.conftest` rather than
rebuilt here — the same cross-module fixture reuse `tests/modules/norms/conftest.py`
already does for gis's fixtures.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications.models import Application
from app.modules.auth.models import Applicant
from app.modules.payments.models import Invoice
from tests.modules.applications.conftest import applicant as applicant


@pytest.fixture
async def approved_application(db: AsyncSession, applicant: Applicant) -> Application:
    """An application already in APPROVED status — the state payments hangs off.
    Mirrors `_draft` (`test_public_surface.py`) with `status` set directly."""
    row = Application(
        applicant_id=applicant.id,
        submitted_by_user_id=applicant.owner_user_id,
        on_behalf="self",
        channel="portal",
        status="APPROVED",
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def invoice(db: AsyncSession, approved_application: Application) -> Invoice:
    """A single pending invoice for `approved_application` — what the third brief
    test's `ProviderTransaction` rows point `invoice_id` at (ruling P1)."""
    row = Invoice(
        application_id=approved_application.id,
        number=f"INV-2027-{uuid.uuid4().hex[:6]}",
        amount=Decimal("100.00"),
        status="pending",
    )
    db.add(row)
    await db.flush()
    return row
