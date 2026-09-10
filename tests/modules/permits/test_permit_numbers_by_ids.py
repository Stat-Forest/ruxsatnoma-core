"""Stage 13 (ruling #204): the batch reader other modules' exports print a
permit by — the display number, one query, `{}` for an empty set."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.permits import service
from app.modules.permits.models import Permit
from app.modules.permits.service import _permit_number

pytestmark = pytest.mark.asyncio


class _NoQueries:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - reached only on a defect
        raise AssertionError("an empty id set must issue no query")


async def test_permit_numbers_by_ids_prints_the_display_number(
    db: AsyncSession, issued_permit: Permit
) -> None:
    found = await service.permit_numbers_by_ids(db, {issued_permit.id, uuid.uuid4()})
    assert found == {issued_permit.id: _permit_number(issued_permit.series, issued_permit.number)}
    assert found[issued_permit.id].startswith(issued_permit.series)


async def test_an_empty_set_issues_no_query() -> None:
    assert await service.permit_numbers_by_ids(_NoQueries(), set()) == {}  # type: ignore[arg-type]
