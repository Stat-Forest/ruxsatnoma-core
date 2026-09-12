"""Stage 13 (ruling #204): the two batch readers other modules' exports use
to print an application's number and find its applicant."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.applications import service
from app.modules.applications.models import Application

pytestmark = pytest.mark.asyncio


class _NoQueries:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - reached only on a defect
        raise AssertionError("an empty id set must issue no query")


async def test_numbers_and_applicants_by_ids(db: AsyncSession, submitted_application: str):
    app_id = uuid.UUID(submitted_application)
    row = await db.get(Application, app_id)
    assert row is not None and row.number

    assert await service.numbers_by_ids(db, {app_id, uuid.uuid4()}) == {app_id: row.number}
    assert await service.applicants_by_ids(db, {app_id}) == {app_id: row.applicant_id}


async def test_empty_sets_issue_no_query():
    assert await service.numbers_by_ids(_NoQueries(), set()) == {}  # type: ignore[arg-type]
    assert await service.applicants_by_ids(_NoQueries(), set()) == {}  # type: ignore[arg-type]
