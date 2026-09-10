"""Stage 13 (ruling #204): contour numbers for a batch of ids, one query."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.gis import service
from tests.modules.gis.conftest import make_contour

pytestmark = pytest.mark.asyncio


class _NoQueries:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - reached only on a defect
        raise AssertionError("an empty id set must issue no query")


async def test_contour_numbers_by_ids_resolves_a_batch(db: AsyncSession, contours_layer, leshoz):
    a = await make_contour(db, contours_layer, leshoz)
    b = await make_contour(db, contours_layer, leshoz)
    numbers = await service.contour_numbers_by_ids(db, {a.id, b.id, uuid.uuid4()})
    assert numbers == {a.id: a.number, b.id: b.number}


async def test_empty_set_issues_no_query():
    assert await service.contour_numbers_by_ids(_NoQueries(), set()) == {}  # type: ignore[arg-type]
