"""Fixtures for the applications module.

Reuses the gis primitives rather than re-implementing contour insertion
(`tests/modules/gis/conftest.py` exports `make_contour`, `make_version`,
`random_box_wkt`, `contours_layer`, `leshoz`, `approval_doc` as plain
importables — the same idiom `tests/modules/norms/conftest.py` already uses).

`published_contour` here mirrors `tests/modules/norms/conftest.py`'s own fixture
of the same name, not gis's: it returns the `Contour` (whose `.id` is what
`applications.contour_id` points at), not the `ContourVersion` gis's fixture
returns — the two sibling test packages deliberately give this name different
shapes, each matching what their own module's FK expects.

No `_app_on_test_db` fixture: branch 1 (this task) drives no HTTP requests, only
direct-ORM tests against the `db` fixture."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import MediaFile
from app.modules.auth.models import Applicant
from app.modules.gis.models import Contour, GisLayer
from tests.modules.auth.test_sessions import make_user
from tests.modules.gis.conftest import approval_doc as approval_doc
from tests.modules.gis.conftest import contours_layer as contours_layer
from tests.modules.gis.conftest import leshoz as leshoz
from tests.modules.gis.conftest import make_contour, make_version, random_box_wkt


def unique_pinfl() -> str:
    # Leading digit 1: 2/3/4/5/6/7/8/9 are already claimed by other test modules
    # sharing this same persistent test DB (see tests/modules/gis/conftest.py's
    # own comment on the same convention).
    return f"1{uuid.uuid4().int % 10**13:013d}"


@pytest.fixture
async def applicant(db: AsyncSession) -> Applicant:
    """A fully registered individual applicant, owned by a real user —
    `applications.applicant_id`/`submitted_by_user_id` are both NOT NULL FKs, so a
    bare `uuid7()` would fail the FK before whatever the test means to exercise."""
    user = await make_user(db, role_code="applicant", pinfl=unique_pinfl())
    row = Applicant(kind="individual", pinfl=user.pinfl, name=user.full_name, owner_user_id=user.id)
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def grazing_activity_id(db: AsyncSession) -> uuid.UUID:
    rows = await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    return rows.scalar_one()


@pytest.fixture
async def published_contour(
    db: AsyncSession, contours_layer: GisLayer, leshoz, approval_doc: MediaFile
) -> Contour:
    """A contour whose single version is published, at random coordinates (a fixed
    committed geometry accumulates across runs — lesson). `approval_doc_id` is
    mandatory: `ck_contour_versions_published_needs_doc` rejects a published
    version without one (mirrors norms's and gis's own `published_contour`)."""
    contour = await make_contour(db, contours_layer, leshoz)
    await make_version(
        db, contour.id, random_box_wkt(), status="published", approval_doc_id=approval_doc.id
    )
    await db.flush()
    return contour


@pytest.fixture(autouse=True)
def _isolate_subscriptions():
    """The bus is process-global; without this, a handler registered by one
    test fires inside another and the failure surfaces three files away."""
    from app.core import events

    saved = {name: list(handlers) for name, handlers in events._SUBSCRIBERS.items()}
    yield
    events._SUBSCRIBERS.clear()
    events._SUBSCRIBERS.update(saved)
