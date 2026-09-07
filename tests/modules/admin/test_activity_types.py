"""The catalog's presentation columns (ruling #138), the anonymous public route
that surfaces them, and the one write the hard catalog offers (ruling #139):
`PATCH /refs/activity-types/{id}` edits presentation and switches a service
on/off — never `code` or `quantity_unit`.

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
from app.modules.admin.permissions import CLASSIFIERS_MANAGE
from app.modules.audit.models import AuditLog
from tests.conftest import make_client
from tests.modules.admin.test_organizations_admin import auth_client, signed_in_with

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


@pytest.fixture
async def staff_client(db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated with `admin.classifiers.manage`, the grant the PATCH route
    requires. Built the same way `test_classifiers_admin.py` builds its per-test
    client — `signed_in_with` + `auth_client`, imported from
    `test_organizations_admin` rather than reimplemented here — just wrapped as a
    fixture since this file needs the same authenticated client in four tests."""
    _, token, csrf = await signed_in_with(db, CLASSIFIERS_MANAGE)
    await db.commit()
    async with make_client(create_app(), lifespan=True) as authed:
        auth_client(authed, token, csrf)
        yield authed


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


async def test_patch_edits_presentation_and_writes_an_audit_row(
    db: AsyncSession, staff_client: httpx.AsyncClient
) -> None:
    """Same shared-DB caveat as the archive test below: this mutates the seeded
    `grazing` row for real, and two other tests in this file (and Task 1's own)
    read its seeded `processing_days`/`description` — so the original values are
    captured first and restored in a `finally`, regardless of how the test lands."""
    row = (
        await db.execute(select(ActivityType).where(ActivityType.code == "grazing"))
    ).scalar_one()
    original_processing_days = row.processing_days
    original_description = row.description
    try:
        response = await staff_client.patch(
            f"/api/v1/refs/activity-types/{row.id}",
            json={
                "processing_days": 20,
                "description": {"uz_latn": "Yangi tavsif", "ru": "Новое описание"},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["processing_days"] == 20
        trail = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "activity_type.update")))
            .scalars()
            .all()
        )
        assert trail, "an edit of the catalog left no audit trail"
    finally:
        restored = await staff_client.patch(
            f"/api/v1/refs/activity-types/{row.id}",
            json={"processing_days": original_processing_days, "description": original_description},
        )
        assert restored.status_code == 200, restored.text


async def test_archiving_removes_it_from_both_catalogs(
    db: AsyncSession, staff_client: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    """The test database is shared and persistent across runs — archiving a
    seeded row for real would pass once and fail every run after, so this
    restores the row in a `finally` regardless of how the assertions land."""
    row = (
        await db.execute(select(ActivityType).where(ActivityType.code == "recreation"))
    ).scalar_one()
    try:
        archived = await staff_client.patch(
            f"/api/v1/refs/activity-types/{row.id}", json={"status": "archived"}
        )
        assert archived.status_code == 200, archived.text
        public_codes = [
            item["code"] for item in (await client.get("/api/v1/public/refs/activity-types")).json()
        ]
        staff_codes = [
            item["code"] for item in (await staff_client.get("/api/v1/refs/activity-types")).json()
        ]
        assert "recreation" not in public_codes, (
            "ruling #139a: archived must leave the public catalog"
        )
        assert "recreation" not in staff_codes, (
            "ruling #139a: archived must leave the wizard's catalog too"
        )
    finally:
        restored = await staff_client.patch(
            f"/api/v1/refs/activity-types/{row.id}", json={"status": "active"}
        )
        assert restored.status_code == 200, restored.text


async def test_the_catalog_offers_no_way_to_create_or_delete(
    staff_client: httpx.AsyncClient,
) -> None:
    """Ruling #139: six activities fixed by law. A POST here would be a seventh
    with no tariff, and the calculator resolves tariffs by `code`."""
    assert (await staff_client.post("/api/v1/refs/activity-types", json={})).status_code in (
        404,
        405,
    )


async def test_a_description_without_uz_latn_is_refused(
    db: AsyncSession, staff_client: httpx.AsyncClient
) -> None:
    row = (
        await db.execute(select(ActivityType).where(ActivityType.code == "grazing"))
    ).scalar_one()
    response = await staff_client.patch(
        f"/api/v1/refs/activity-types/{row.id}", json={"description": {"ru": "Только по-русски"}}
    )
    assert response.status_code == 422, "decision #90: uz_latn is required"


def test_nothing_outside_admin_and_norms_reads_processing_days() -> None:
    """Ruling #138a: two numbers meaning "the term" is how a portal promises one
    deadline and enforces another. `SLA_DAYS` stays the only enforced one."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "processing_days" in path.read_text()
        and not path.as_posix().startswith((f"{root}/modules/admin/", f"{root}/modules/norms/"))
    ]
    assert not offenders, f"processing_days read outside admin/norms: {offenders}"
