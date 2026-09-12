"""Stage 13 (ruling #204): the batch name readers the register exports use —
one query per table, `{}` for an empty set without touching the database."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth import service
from app.modules.auth.models import Applicant
from tests.modules.auth.test_sessions import make_user

pytestmark = pytest.mark.asyncio


class _NoQueries:
    async def execute(self, *_args, **_kwargs):  # pragma: no cover - reached only on a defect
        raise AssertionError("an empty id set must issue no query")


async def test_applicant_names_resolves_a_batch(db: AsyncSession):
    users = [
        await make_user(db, role_code="applicant", pinfl=f"3{uuid.uuid4().int % 10**13:013d}")
        for _ in range(2)
    ]
    rows = [
        Applicant(kind="individual", pinfl=u.pinfl, name=f"Applicant {i}", owner_user_id=u.id)
        for i, u in enumerate(users)
    ]
    db.add_all(rows)
    await db.flush()

    names = await service.applicant_names(db, {rows[0].id, rows[1].id, uuid.uuid4()})
    assert names == {rows[0].id: "Applicant 0", rows[1].id: "Applicant 1"}


async def test_user_names_resolves_a_batch(db: AsyncSession):
    a = await make_user(db)
    b = await make_user(db)
    a.full_name, b.full_name = "Alisher Karimov", "Bobur Rahimov"
    await db.flush()
    assert await service.user_names(db, {a.id, b.id}) == {
        a.id: "Alisher Karimov",
        b.id: "Bobur Rahimov",
    }


async def test_empty_sets_issue_no_query():
    assert await service.applicant_names(_NoQueries(), set()) == {}  # type: ignore[arg-type]
    assert await service.user_names(_NoQueries(), set()) == {}  # type: ignore[arg-type]
