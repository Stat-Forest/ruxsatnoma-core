"""Zone scoping for `inspections.view_any` (seam audit, stage 4).

Every other reader in the codebase (`search`, `reports`, `dashboard`,
`archive`) supplies `region_col`/`district_col` — via a join to
`organizations` — alongside `organization_col` when calling
`app.core.abac.zone_filter`, because a viewer's zone can be region- or
district-scoped with NO `organization_id` at all (`admin.users_service.
create_user` can produce one; see `decisions.md`'s GIS zone-gate findings for
the same actor shape). `inspections`'s three list scopes
(`_task_scope`/`_act_scope`/`_case_scope`) originally supplied
`organization_col` alone, which made `zone_filter` raise `ValueError` — a 500,
not a scoped result — for exactly that actor shape, since `zone_filter` fails
closed on a zone field with no matching column."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import Organization, Region
from app.modules.applications.models import Application
from tests.modules.auth.test_sessions import make_user
from tests.modules.inspections.conftest import _client_for_user, unique_pinfl

API = "/api/v1/inspections"


@pytest.fixture
async def fergana_id(db: AsyncSession) -> uuid.UUID:
    return (await db.execute(select(Region.id).where(Region.code == "fergana"))).scalar_one()


@pytest.fixture
async def andijan_id(db: AsyncSession) -> uuid.UUID:
    return (await db.execute(select(Region.id).where(Region.code == "andijan"))).scalar_one()


@pytest.fixture
async def leshoz_in_fergana(db: AsyncSession, leshoz: Organization, fergana_id: uuid.UUID):
    leshoz.region_id = fergana_id
    await db.flush()
    return leshoz


async def region_prosecutor(db: AsyncSession, region_id: uuid.UUID):
    """A `prosecutor` with a REGION zone and no organization — the actor
    shape this finding is about; `inspections.view_any` is seeded to
    `prosecutor` by migration `0026`."""
    return await make_user(db, role_code="prosecutor", region_id=region_id, pinfl=unique_pinfl())


async def test_region_scoped_view_any_lists_tasks_in_zone(
    db: AsyncSession,
    leshoz_in_fergana: Organization,
    fergana_id: uuid.UUID,
    executor_head_client,
    application: Application,
    inspector,
) -> None:
    created = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    prosecutor = await region_prosecutor(db, fergana_id)
    async with _client_for_user(db, prosecutor) as client:
        r = await client.get(f"{API}/tasks")
    assert r.status_code == 200, r.text
    assert task_id in {row["id"] for row in r.json()["items"]}


async def test_region_scoped_view_any_excludes_other_region(
    db: AsyncSession,
    leshoz_in_fergana: Organization,
    andijan_id: uuid.UUID,
    executor_head_client,
    application: Application,
    inspector,
) -> None:
    """The fail-CLOSED half: a prosecutor scoped to a DIFFERENT region must
    not see a task in `leshoz_in_fergana` — proves the fix scopes rather than
    merely avoiding the crash."""
    created = await executor_head_client.post(
        f"{API}/tasks",
        json={
            "kind": "permit_inspection",
            "application_id": str(application.id),
            "assigned_to": str(inspector.id),
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    prosecutor = await region_prosecutor(db, andijan_id)
    async with _client_for_user(db, prosecutor) as client:
        r = await client.get(f"{API}/tasks")
    assert r.status_code == 200, r.text
    assert task_id not in {row["id"] for row in r.json()["items"]}


async def test_region_scoped_view_any_lists_acts(
    db: AsyncSession,
    leshoz_in_fergana: Organization,
    fergana_id: uuid.UUID,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
) -> None:
    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": True},
            "result": "compliant",
        },
    )
    assert created.status_code == 201, created.text
    act_id = created.json()["id"]

    prosecutor = await region_prosecutor(db, fergana_id)
    async with _client_for_user(db, prosecutor) as client:
        r = await client.get(f"{API}/acts")
    assert r.status_code == 200, r.text
    assert act_id in {row["id"] for row in r.json()["items"]}


async def test_region_scoped_view_any_lists_cases(
    db: AsyncSession,
    leshoz_in_fergana: Organization,
    fergana_id: uuid.UUID,
    inspector,
    inspector_client,
    application: Application,
    default_checklist_id: uuid.UUID,
    vt_01: uuid.UUID,
) -> None:
    from app.modules.inspections import repo as inspections_repo
    from app.modules.inspections import service as inspections_service
    from app.modules.integrations.adapters.eimzo import encode_mock_signature

    created = await inspector_client.post(
        f"{API}/acts",
        json={
            "application_id": str(application.id),
            "occurred_at": "2027-06-01T10:00:00Z",
            "checklist_id": str(default_checklist_id),
            "answers": {"activity_matches": True, "within_contour": False},
            "result": "violation",
        },
    )
    assert created.status_code == 201, created.text
    act_id = created.json()["id"]
    act = await inspections_repo.get_act(db, uuid.UUID(act_id))
    assert act is not None

    pkcs7 = encode_mock_signature(
        document=inspections_service._act_package_bytes(act),
        serial=f"SN-{inspector.pinfl}",
        issuer="ISS-1",
        pinfl=inspector.pinfl,
    )
    signed = await inspector_client.post(
        f"{API}/acts/{act_id}/sign",
        json={"pkcs7": pkcs7, "violation_type_item_id": str(vt_01)},
    )
    assert signed.status_code == 200, signed.text

    case = await inspections_repo.case_for_act(db, act.id)
    assert case is not None

    prosecutor = await region_prosecutor(db, fergana_id)
    async with _client_for_user(db, prosecutor) as client:
        r = await client.get(f"{API}/cases")
    assert r.status_code == 200, r.text
    assert str(case.id) in {row["id"] for row in r.json()["items"]}
