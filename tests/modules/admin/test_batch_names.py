"""Stage 13 (ruling #204): batch name readers over the reference catalogues."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin import service
from tests.modules.gis.conftest import leshoz as leshoz  # noqa: F401
from tests.modules.gis.conftest import other_leshoz as other_leshoz  # noqa: F401

pytestmark = pytest.mark.asyncio


class _NoQueries:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - reached only on a defect
        raise AssertionError("an empty id set must issue no query")


async def test_organization_names_resolves_a_batch(db: AsyncSession, leshoz, other_leshoz):  # noqa: F811
    names = await service.organization_names(db, {leshoz.id, other_leshoz.id, uuid.uuid4()})
    assert set(names) == {leshoz.id, other_leshoz.id}
    assert names[leshoz.id] == dict(leshoz.name) and names[leshoz.id]


async def test_activity_type_names_is_the_whole_catalogue(db: AsyncSession):
    grazing = (
        await db.execute(text("SELECT id FROM activity_types WHERE code = 'grazing'"))
    ).scalar_one()
    names = await service.activity_type_names(db)
    assert grazing in names
    assert names[grazing]["uz_latn"]


async def test_empty_set_issues_no_query():
    assert await service.organization_names(_NoQueries(), set()) == {}  # type: ignore[arg-type]
