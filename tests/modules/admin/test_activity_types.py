"""The catalog's presentation columns (ruling #138) and the anonymous public
route that surfaces them. The PATCH route a later task adds (ruling #139) is
not here yet — this file only widens the row, seeds it, and proves the two
output schemas carry the new fields.

`description`/`processing_days` are seeded by migration `0038` from the copy
`landing/src/i18n/{ru,uz_latn}/services.ts` carried under its own
`services.items.<name>.desc` keys. Those keys are camelCase
(`grazing`/`haymaking`/`beekeeping`/`wildPlants`/`recreation`) and map onto
`activity_types.code` by MEANING, not by string equality — `SELECT code FROM
activity_types` gives `grazing`, `haymaking`, `apiary`, `recreation`,
`deadwood`, `science`. `beekeeping` is `apiary`; `wildPlants` (gathering wild
fruit/nuts/herbs) has no counterpart among the six codes — `deadwood` is
specifically dry-branch collection, a different activity — so it is not
force-matched to it. That leaves `deadwood` and `science` with no seeded
description at all: the migration leaves both NULL rather than inventing
copy, and this file asserts that absence explicitly rather than only
asserting presence for the four matched rows.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.admin.models import ActivityType
from tests.conftest import make_client

DESCRIBED_CODES = {"grazing", "haymaking", "apiary", "recreation"}
UNDESCRIBED_CODES = {"deadwood", "science"}


@pytest.fixture
async def client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """No session cookie — `GET /public/refs/activity-types` (`norms.
    public_router`) is the anonymous surface the landing site reads, and its
    whole contract is that it needs no login. Local to this file rather than
    added to `tests/modules/admin/conftest.py`: no other admin test drives an
    anonymous client, and the seeded rows this test reads are already
    committed by the migration, so there is nothing pending on `db` a
    request-time commit hook would need to flush first."""
    async with make_client(create_app(), lifespan=True) as anonymous:
        yield anonymous


async def test_every_seeded_activity_has_a_term_and_the_described_ones_have_a_description(
    db: AsyncSession,
) -> None:
    rows = list((await db.execute(select(ActivityType))).scalars())
    assert len(rows) == 6
    by_code = {row.code: row for row in rows}
    assert set(by_code) == DESCRIBED_CODES | UNDESCRIBED_CODES

    for row in rows:
        assert row.processing_days == 15, f"{row.code} was seeded with a term other than 15"

    for code in DESCRIBED_CODES:
        description = by_code[code].description
        assert description is not None, f"{code} has no description"
        assert description.get("uz_latn", "").strip(), f"{code} has no uz_latn description"
        assert description.get("ru", "").strip(), f"{code} has no ru description"

    for code in UNDESCRIBED_CODES:
        assert by_code[code].description is None, (
            f"{code} has no counterpart in the landing copy and must stay NULL, not invented"
        )


async def test_public_refs_carry_the_presentation_fields(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/public/refs/activity-types")
    assert response.status_code == 200
    body = response.json()
    assert body, "the anonymous catalog answered an empty list"
    for row in body:
        assert "description" in row and "processing_days" in row
        assert "status" not in row and "quantity_unit" not in row
    grazing = next(row for row in body if row["code"] == "grazing")
    assert grazing["processing_days"] == 15
    assert grazing["description"]["uz_latn"].strip()
    assert grazing["description"]["ru"].strip()
    science = next(row for row in body if row["code"] == "science")
    assert science["description"] is None
